import asyncio
import inspect
import json
import re
from collections.abc import AsyncIterator

import pytest

from tests.conftest import run_end_contract_fields, run_start_permission_fields
from tests.support.capability import preview_capability_plan
from tests.support.runtime_session import in_memory_runtime_session

from pulsara_agent.event import (
    CapabilityExposureResolvedEvent,
    AgentEvent,
    CapabilityGateDecisionEvent,
    ChildRolloutSubaccountClosedEvent,
    ContextCompiledEvent,
    CustomEvent,
    EventContext,
    ModelCallEndEvent,
    ModelCallStartEvent,
    RunEndEvent,
    RunStartEvent,
    RolloutBudgetReservationSettledEvent,
    SubagentEdgeRecordedEvent,
    SubagentMessageSentEvent,
    SubagentPhaseReportedEvent,
    SubagentResultConsumedEvent,
    SubagentResultDeliveredEvent,
    SubagentResultSubmittedEvent,
    SubagentRunFailedEvent,
    SubagentRunCancelledEvent,
    SubagentRunCompletedEvent,
    SubagentRunStartedEvent,
    SubagentTaskBlockedEvent,
    SubagentTaskCancelledEvent,
    SubagentTaskCompletedEvent,
    SubagentTaskCreatedEvent,
    SubagentTaskFailedEvent,
    SubagentTaskScheduledEvent,
    SubagentTaskStartedEvent,
    TextBlockEndEvent,
    TextBlockDeltaEvent,
    TextBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from pulsara_agent.event_log import InMemoryEventLog, dump_agent_event, load_agent_event
from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.host.identity import HostWorkspaceInput, resolve_workspace
from pulsara_agent.host.session import HostSession
from pulsara_agent.llm import LLMRuntime
from tests.support import (
    model_call_start_fields,
    run_agent_task,
    test_llm_config,
    test_model_limits,
)
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.message import ToolResultState
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.runtime.subagent import (
    InMemoryEventLogLocator,
    SubagentBudget,
    SubagentLimitExceeded,
    SubagentRuntime,
)
from pulsara_agent.runtime.mcp.types import McpBindingIdentity
from pulsara_agent.runtime.subagent.execution import ChildExecutionRegistry
from pulsara_agent.runtime.subagent.run_entry import SubagentRunEntryDriver
from pulsara_agent.runtime import (
    AgentRuntime,
    EventWriteConflict,
    LoopStatus,
    RuntimeSession,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.run_lifecycle import RunStopReason
from pulsara_agent.runtime.permission import preset_to_policy
from pulsara_agent.runtime.wiring import (
    AgentRuntimeWiring,
    build_in_memory_runtime_wiring,
)
from pulsara_agent.runtime.transcript import rebuild_prior_messages
from pulsara_agent.runtime.context_input.event_slice import ContextEventSlice
from pulsara_agent.runtime.context_input.replay import (
    load_context_input_manifest,
    replay_compiled_context,
)
from pulsara_agent.tools.base import ToolCall, ToolRuntimeContext
from pulsara_agent.tools.builtins.subagent import (
    CreateAgentTasksTool,
    ListAgentsTool,
    ReportAgentResultTool,
    SpawnAgentTool,
    StopAgentTaskTool,
    WaitAgentTool,
    WaitAgentTasksTool,
)


CTX = EventContext(run_id="run:parent", turn_id="turn:parent", reply_id="reply:parent")

_SUBAGENT_TEST_MODEL_LIMITS = test_model_limits(
    total_context_tokens=512_000,
    max_input_tokens=512_000,
    input_safety_margin_tokens=16_000,
)


def _subagent_test_llm_config(**kwargs):
    """Use a model pair whose frozen rollout policy can fund one child call."""

    return test_llm_config(
        **kwargs,
        pro_limits=_SUBAGENT_TEST_MODEL_LIMITS,
        flash_limits=_SUBAGENT_TEST_MODEL_LIMITS,
    )


def _append_parent_run_start(
    event_log: InMemoryEventLog,
    *,
    runtime_session_id: str,
    event_context: EventContext,
    user_input: str = "",
) -> RunStartEvent:
    existing = next(
        (
            event
            for event in event_log.iter(run_id=event_context.run_id)
            if isinstance(event, RunStartEvent)
        ),
        None,
    )
    if existing is not None:
        return existing
    prior = event_log.iter()
    start = RunStartEvent(
        **event_context.event_fields(),
        **run_start_permission_fields(
            event_context.run_id,
            user_input=user_input,
            turn_id=event_context.turn_id,
            reply_id=event_context.reply_id,
            mcp_installation_owner_runtime_session_id=runtime_session_id,
            transcript_source_through_sequence=max(
                (event.sequence or 0 for event in prior), default=0
            ),
            transcript_source_event_count=len(prior),
        ),
        user_input_chars=len(user_input),
    )
    stored = event_log.append(start)
    assert isinstance(stored, RunStartEvent)
    return stored


async def _emit_parent_run_start(
    runtime_session: RuntimeSession,
    *,
    event_context: EventContext,
    user_input: str = "",
) -> RunStartEvent:
    existing = next(
        (
            event
            for event in runtime_session.event_log.iter(run_id=event_context.run_id)
            if isinstance(event, RunStartEvent)
        ),
        None,
    )
    if existing is not None:
        return existing
    prior = runtime_session.event_log.iter()
    stored = await runtime_session.emit(
        RunStartEvent(
            **event_context.event_fields(),
            **run_start_permission_fields(
                event_context.run_id,
                user_input=user_input,
                turn_id=event_context.turn_id,
                reply_id=event_context.reply_id,
                mcp_installation_owner_runtime_session_id=(
                    runtime_session.runtime_session_id
                ),
                transcript_source_through_sequence=max(
                    (event.sequence or 0 for event in prior), default=0
                ),
                transcript_source_event_count=len(prior),
            ),
            user_input_chars=len(user_input),
        )
    )
    assert isinstance(stored, RunStartEvent)
    return stored


def _runtime(
    tmp_path,
    *,
    budget: SubagentBudget | None = None,
    child_runner=None,
    seed_parent_run: bool = True,
):
    event_log = InMemoryEventLog(runtime_session_id="runtime:parent")
    parent = in_memory_runtime_session(
        tmp_path,
        runtime_session_id="runtime:parent",
        event_log=event_log,
    )
    if seed_parent_run:
        _append_parent_run_start(
            event_log,
            runtime_session_id="runtime:parent",
            event_context=CTX,
        )
    locator = InMemoryEventLogLocator()
    child_logs: dict[str, InMemoryEventLog] = {}

    def child_event_log_factory(runtime_session_id: str) -> InMemoryEventLog:
        event_log = InMemoryEventLog()
        child_logs[runtime_session_id] = event_log
        locator.register(runtime_session_id, event_log)
        return event_log

    runtime = SubagentRuntime(
        parent_runtime_session=parent,
        child_event_log_factory=child_event_log_factory,
        event_log_locator=locator,
        default_budget=budget,
        child_runner=child_runner,
    )
    return parent, locator, child_logs, runtime


def _resumed_runtime(parent, locator, child_logs):
    resumed_parent = RuntimeSession(
        parent.workspace_root,
        event_log=parent.event_log,
        archive=parent.archive,
        tool_result_artifacts=parent.tool_result_artifacts,
        runtime_session_id=parent.runtime_session_id,
        terminal_binding=parent.terminal_binding,
        extra_tool_bindings=parent.extra_tool_bindings,
    )
    resumed = SubagentRuntime(
        parent_runtime_session=resumed_parent,
        child_event_log_factory=lambda runtime_session_id: child_logs.setdefault(
            runtime_session_id,
            InMemoryEventLog(),
        ),
        event_log_locator=locator,
    )
    return resumed_parent, resumed


def test_child_registry_indexes_exact_mcp_binding_identities() -> None:
    registry = ChildExecutionRegistry()
    identity = McpBindingIdentity(
        server_id="docs",
        slot_id="mcp_slot:1",
        snapshot_id="mcp_snapshot:1",
        discovery_generation=1,
    )
    registry.register_prepared(
        subagent_run_id="subagent_run:mcp",
        child_runtime_session_id="runtime:child:mcp",
        child_session=None,
        reservation=None,
        mcp_binding_identities=frozenset({identity}),
    )

    assert registry.child_ids_for_mcp_bindings(frozenset({identity})) == frozenset(
        {"subagent_run:mcp"}
    )
    registry.release_handle("subagent_run:mcp")
    assert registry.child_ids_for_mcp_bindings(frozenset({identity})) == frozenset()


def test_child_runtime_mcp_installation_owner_points_to_parent_session(
    tmp_path,
) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    parent.set_mcp_installation_contract(installation_id="mcp_installation:parent")

    child = runtime._create_child_runtime_session(  # noqa: SLF001
        child_runtime_session_id="runtime:child:mcp-owner",
        subagent_run_id="subagent_run:mcp-owner",
        parent_run_id="run:parent",
        capability_profile_id="subagent_capability_profile:test",
    )

    assert child.mcp_installation_id == "mcp_installation:parent"
    assert child.mcp_installation_owner_runtime_session_id == parent.runtime_session_id


def test_subagent_graph_events_are_parent_stream_and_task_is_artifact_backed(
    tmp_path,
) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)

    async def run() -> None:
        subagent = await runtime.spawn_fake(
            task="Implement the isolated worker task.\n" * 30,
            event_context=CTX,
            label="worker-a",
            parent_context_id="context:parent:1",
            parent_model_call_index=2,
            spawning_tool_name="spawn_agent",
            spawn_initiator_kind="tool_call",
            spawn_initiator_id="tool:spawn",
        )
        events = [
            event
            for event in parent.event_log.iter()
            if not isinstance(event, RunStartEvent)
        ]
        assert [type(event) for event in events] == [
            SubagentRunStartedEvent,
            SubagentMessageSentEvent,
        ]

        started = events[0]
        assert isinstance(started, SubagentRunStartedEvent)
        assert started.type == "SUBAGENT_RUN_STARTED"
        assert started.parent_context_id == "context:parent:1"
        assert started.parent_model_call_index == 2
        assert started.spawn_initiator_kind == "tool_call"
        assert started.spawn_initiator_id == "tool:spawn"
        assert started.child_runtime_session_id == subagent.child_runtime_session_id
        assert len(started.task_preview) <= 500

        task_artifact_id = f"{subagent.subagent_run_id}:task"
        assert parent.archive.get_text(
            task_artifact_id, session_id=parent.runtime_session_id
        ).startswith("Implement the isolated worker task.")

        graph = runtime.graph()
        assert len(graph.nodes) == 1
        assert graph.nodes[0].status == "running"
        assert len(graph.edges) == 1
        assert graph.edges[0].edge_kind == "spawn"
        assert graph.edges[0].payload_artifact_id == task_artifact_id

    asyncio.run(run())


def test_spawn_observer_failure_does_not_duplicate_child(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)

    class FailingObserver:
        async def on_published_event(self, _published) -> None:
            raise RuntimeError("synthetic observer failure")

    parent.publisher.subscribe(FailingObserver())

    async def run() -> None:
        spawned = await runtime.spawn_fake(task="single child", event_context=CTX)
        assert spawned.status == "running"

    asyncio.run(run())
    assert len(runtime.runs) == 1
    assert [
        type(event)
        for event in parent.event_log.iter()
        if not isinstance(event, RunStartEvent)
    ] == [
        SubagentRunStartedEvent,
        SubagentMessageSentEvent,
    ]


def test_child_start_failure_emits_terminal_repair(tmp_path, monkeypatch) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)

    def fail_child_session(**_kwargs):
        raise RuntimeError("synthetic child runtime construction failure")

    monkeypatch.setattr(runtime, "_create_child_runtime_session", fail_child_session)

    async def run() -> None:
        with pytest.raises(RuntimeError, match="synthetic child"):
            await runtime.spawn_fake(task="cannot start", event_context=CTX)

    asyncio.run(run())
    [run_fact] = runtime.runs
    assert run_fact.status == "failed"
    failed_event = next(
        event
        for event in parent.event_log.iter()
        if isinstance(event, SubagentRunFailedEvent)
    )
    assert failed_event.reason_code == "subagent_child_start_failed"
    assert failed_event.repair_id is not None
    assert runtime._execution_registry.uncommitted_reservation_count() == 0  # noqa: SLF001


def test_materialized_batch_facts_are_applied_once(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    tool = CreateAgentTasksTool(runtime)

    async def run() -> None:
        result = await tool.execute_async(
            ToolCall(
                id="tool:materialize-once",
                name="create_agent_tasks",
                arguments={
                    "tasks": [
                        {"task_key": "a", "profile": "review_worker", "task": "A"},
                        {"task_key": "b", "profile": "review_worker", "task": "B"},
                    ]
                },
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id,
                event_context=CTX,
            ),
        )
        assert result.status is ToolResultState.SUCCESS

    asyncio.run(run())
    events = parent.event_log.iter()
    assert len({event.id for event in events}) == len(events)
    assert runtime._graph_store.through_sequence == events[-1].sequence  # noqa: SLF001
    assert len(runtime.tasks) == 2
    assert len(runtime.runs) == 2


def test_child_raw_events_get_subagent_metadata_at_runtime_session_boundary(
    tmp_path,
) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)

    async def run() -> None:
        subagent = await runtime.spawn_fake(task="child task", event_context=CTX)
        child = runtime.child_runtime_session(subagent.subagent_run_id)
        child_ctx = EventContext(
            run_id="run:child", turn_id="turn:child", reply_id="reply:child"
        )

        stored = await child.emit(
            TextBlockDeltaEvent(
                **child_ctx.event_fields(), block_id="text:1", delta="hello"
            )
        )

        assert (
            stored.metadata["subagent"]["subagent_run_id"] == subagent.subagent_run_id
        )
        assert (
            stored.metadata["subagent"]["parent_runtime_session_id"]
            == parent.runtime_session_id
        )
        assert stored.metadata["subagent"]["parent_run_id"] == CTX.run_id
        assert (
            stored.metadata["subagent"]["capability_profile_id"]
            == subagent.capability_profile.profile_id
        )
        assert all(
            not isinstance(event, TextBlockDeltaEvent)
            for event in parent.event_log.iter()
        )

    asyncio.run(run())


def test_child_publish_stored_event_requires_boundary_metadata(tmp_path) -> None:
    _parent, _locator, _child_logs, runtime = _runtime(tmp_path)

    async def run() -> None:
        subagent = await runtime.spawn_fake(task="child task", event_context=CTX)
        child = runtime.child_runtime_session(subagent.subagent_run_id)
        child_ctx = EventContext(
            run_id="run:child", turn_id="turn:child", reply_id="reply:child"
        )
        directly_appended = child.event_log.append(
            TextBlockDeltaEvent(
                **child_ctx.event_fields(), block_id="text:1", delta="missing metadata"
            )
        )

        with pytest.raises(ValueError, match="default metadata"):
            child.publish_stored_event(directly_appended)

    asyncio.run(run())


def test_child_publish_stored_event_requires_nested_subagent_metadata_values(
    tmp_path,
) -> None:
    _parent, _locator, _child_logs, runtime = _runtime(tmp_path)

    async def run() -> None:
        subagent = await runtime.spawn_fake(task="child task", event_context=CTX)
        child = runtime.child_runtime_session(subagent.subagent_run_id)
        child_ctx = EventContext(
            run_id="run:child", turn_id="turn:child", reply_id="reply:child"
        )
        directly_appended = child.event_log.append(
            TextBlockDeltaEvent(
                **child_ctx.event_fields(),
                block_id="text:1",
                delta="shallow metadata",
                metadata={"subagent": {}},
            )
        )

        with pytest.raises(ValueError, match="default metadata"):
            child.publish_stored_event(directly_appended)

    asyncio.run(run())


def test_wait_result_records_consumption_edge_without_delivered_event(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)

    async def run() -> None:
        subagent = await runtime.spawn_fake(task="child task", event_context=CTX)
        result = await runtime.complete_fake(
            subagent.subagent_run_id, summary="child finished", event_context=CTX
        )
        wait_context = EventContext(
            run_id="run:wait", turn_id="turn:wait", reply_id="reply:wait"
        )
        await _emit_parent_run_start(
            parent,
            event_context=wait_context,
        )
        waited = await runtime.wait_result(
            subagent.subagent_run_id,
            event_context=wait_context,
            returned_to_tool_call_id="tool:wait",
            source_context_id="context:wait",
            source_model_call_index=3,
        )

        assert waited == result
        events = parent.event_log.iter()
        assert any(isinstance(event, SubagentRunCompletedEvent) for event in events)
        assert any(isinstance(event, SubagentEdgeRecordedEvent) for event in events)
        assert not any(
            isinstance(event, SubagentResultDeliveredEvent) for event in events
        )

        wait_edge = next(
            event for event in events if isinstance(event, SubagentEdgeRecordedEvent)
        )
        assert wait_edge.edge_kind == "wait"
        assert wait_edge.result_id == result.result_id
        assert wait_edge.result_artifact_id == result.final_message_artifact_id
        assert wait_edge.returned_to_tool_call_id == "tool:wait"

        graph = runtime.graph()
        assert graph.nodes[0].status == "completed"
        assert graph.nodes[0].consumed_by_wait is True
        assert graph.nodes[0].delivered is False

    asyncio.run(run())


def test_wait_agent_failed_run_returns_terminal_outcome_without_fake_consumption(
    tmp_path,
) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)

    async def run() -> None:
        child = await runtime.spawn_fake(task="will fail", event_context=CTX)
        await runtime.fail(
            child.subagent_run_id,
            reason_code="child_failed_for_test",
            event_context=CTX,
        )
        result = await WaitAgentTool(runtime).execute_async(
            ToolCall(
                id="tool:wait-failed",
                name="wait_agent",
                arguments={"subagent_run_id": child.subagent_run_id},
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id,
                event_context=CTX,
            ),
        )
        payload = json.loads(result.output)
        assert payload["status"] == "failed"
        assert payload["reason_code"] == "child_failed_for_test"
        assert payload["terminal_event_id"]
        assert payload["result_id"] is None

    asyncio.run(run())
    assert not any(
        isinstance(event, (SubagentEdgeRecordedEvent, SubagentResultConsumedEvent))
        for event in parent.event_log.iter()
    )


def test_spawn_agent_tool_rejects_invalid_role_and_context_before_persisting(
    tmp_path,
) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    tool = SpawnAgentTool(runtime)

    async def run() -> None:
        result = await tool.execute_async(
            ToolCall(
                id="tool:spawn",
                name="spawn_agent",
                arguments={
                    "task": "child task",
                    "role": "wizard",
                    "context": "telepathic",
                },
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id, event_context=CTX
            ),
        )
        assert result.status.value == "error"
        payload = json.loads(result.output)
        assert payload["status"] == "error"
        assert "role must be one of" in payload["error"]

    asyncio.run(run())

    assert not any(
        isinstance(event, SubagentRunStartedEvent) for event in parent.event_log.iter()
    )


def test_list_agents_tool_returns_bounded_run_only_projection(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    tool = ListAgentsTool(runtime)

    async def run() -> None:
        subagent = await runtime.spawn_fake(
            task="child task", event_context=CTX, label="worker-a"
        )
        await runtime.complete_fake(
            subagent.subagent_run_id, summary="child finished", event_context=CTX
        )

        result = await tool.execute_async(
            ToolCall(
                id="tool:list", name="list_agents", arguments={"include_edges": True}
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id, event_context=CTX
            ),
        )

        assert result.status is ToolResultState.SUCCESS
        payload = json.loads(result.output)
        assert payload["status"] == "ok"
        assert payload["total_items"] == 1
        [item] = payload["items"]
        assert item["item_kind"] == "run"
        assert item["task_id"] is None
        assert item["subagent_run_id"] == subagent.subagent_run_id
        assert item["status"] == "completed"
        assert item["label"] == "worker-a"
        assert item["result_id"]
        assert "child finished" not in result.output
        assert payload["edges"]

    asyncio.run(run())


def test_list_projection_does_not_hydrate_full_child_transcript(
    tmp_path, monkeypatch
) -> None:
    parent, locator, _child_logs, runtime = _runtime(tmp_path)
    tool = ListAgentsTool(runtime)

    async def run() -> None:
        subagent = await runtime.spawn_fake(
            task="child task", event_context=CTX, label="worker-a"
        )
        child_session = runtime.child_runtime_session(subagent.subagent_run_id)
        child_context = EventContext(
            run_id="run:child-list-boundary",
            turn_id="turn:child-list-boundary",
            reply_id="reply:child-list-boundary",
        )
        await child_session.emit(
            CustomEvent(
                **child_context.event_fields(),
                name="child_raw_transcript_marker",
                value={"secret_marker": "MUST_NOT_BE_HYDRATED_BY_LIST"},
            )
        )

        def fail_if_child_log_is_iterated(_event_log, *_args, **_kwargs):
            raise AssertionError("list_agents must not hydrate the child event stream")

        monkeypatch.setattr(InMemoryEventLog, "iter", fail_if_child_log_is_iterated)
        result = await tool.execute_async(
            ToolCall(id="tool:list-no-hydration", name="list_agents", arguments={}),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id,
                event_context=CTX,
            ),
        )

        assert result.status is ToolResultState.SUCCESS
        assert "MUST_NOT_BE_HYDRATED_BY_LIST" not in result.output

    asyncio.run(run())


def test_subagent_task_can_exist_without_child_run_and_projects_to_list_agents(
    tmp_path,
) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    tool = ListAgentsTool(runtime)

    async def run() -> None:
        task = await runtime.create_task(
            objective="Review the parser implementation.",
            event_context=CTX,
            profile_id="review_worker",
            task_key="review",
            label="Review",
            display_role="reviewer",
        )

        assert task.status == "created"
        assert task.has_child_run is False
        assert [
            type(event)
            for event in parent.event_log.iter()
            if not isinstance(event, RunStartEvent)
        ] == [
            SubagentTaskCreatedEvent
        ]
        graph = runtime.graph()
        assert len(graph.tasks) == 1
        assert graph.tasks[0].task_id == task.task_id
        assert graph.tasks[0].status == "created"
        assert graph.tasks[0].current_run_id is None
        assert graph.nodes == ()

        result = await tool.execute_async(
            ToolCall(id="tool:list", name="list_agents", arguments={}),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id, event_context=CTX
            ),
        )
        payload = json.loads(result.output)
        assert payload["total_items"] == 1
        [item] = payload["items"]
        assert item["item_kind"] == "task"
        assert item["task_id"] == task.task_id
        assert item["subagent_run_id"] is None
        assert item["status"] == "created"
        assert item["profile_id"] == "review_worker"
        assert item["objective_preview"] == "Review the parser implementation."

    asyncio.run(run())


def test_subagent_task_start_links_child_run_without_duplicate_list_item(
    tmp_path,
) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    tool = ListAgentsTool(runtime)

    async def run() -> None:
        task = await runtime.create_task(
            objective="Verify the command line smoke test.",
            event_context=CTX,
            profile_id="verification_worker",
            batch_id="subagent_batch:test",
            create_tool_call_id="tool:create-tasks",
            task_key="verify",
            label="Verify",
        )
        subagent = await runtime.start_task(
            task.task_id,
            event_context=CTX,
            parent_context_id="context:parent",
            parent_model_call_index=7,
            spawn_initiator_kind="tool_call",
            spawn_initiator_id="tool:create-tasks",
        )

        assert subagent.task_id == task.task_id
        assert subagent.batch_id == "subagent_batch:test"
        assert subagent.create_tool_call_id == "tool:create-tasks"
        assert subagent.run_index == 1
        assert subagent.spawn_initiator_kind == "tool_call"
        assert subagent.spawn_initiator_id == "tool:create-tasks"
        event_types = [
            type(event)
            for event in parent.event_log.iter()
            if not isinstance(event, RunStartEvent)
        ]
        assert event_types == [
            SubagentTaskCreatedEvent,
            SubagentTaskScheduledEvent,
            SubagentRunStartedEvent,
            SubagentMessageSentEvent,
            SubagentTaskStartedEvent,
        ]

        graph = runtime.graph()
        assert len(graph.tasks) == 1
        assert graph.tasks[0].status == "running"
        assert graph.tasks[0].current_run_id == subagent.subagent_run_id
        assert graph.tasks[0].has_child_run is True
        assert graph.tasks[0].run_index == 1
        assert len(graph.nodes) == 1

        result = await tool.execute_async(
            ToolCall(id="tool:list", name="list_agents", arguments={}),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id, event_context=CTX
            ),
        )
        payload = json.loads(result.output)
        assert payload["total_items"] == 1
        [item] = payload["items"]
        assert item["item_kind"] == "task"
        assert item["task_id"] == task.task_id
        assert item["subagent_run_id"] == subagent.subagent_run_id
        assert item["current_run_id"] == subagent.subagent_run_id
        assert item["run_index"] == 1

    asyncio.run(run())


def test_concurrent_task_start_has_single_winner(tmp_path, monkeypatch) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)

    async def run() -> None:
        task = await runtime.create_task(
            objective="Start exactly once",
            event_context=CTX,
            profile_id="review_worker",
            batch_id="subagent_batch:concurrent",
            create_tool_call_id="tool:create-concurrent",
        )
        original_commit = runtime._commit_plan  # noqa: SLF001 - force both planners onto one snapshot.
        entered = 0
        both_planned = asyncio.Event()

        async def synchronized_commit(plan):
            nonlocal entered
            if plan.operation == "start_task":
                entered += 1
                if entered == 2:
                    both_planned.set()
                await asyncio.wait_for(both_planned.wait(), timeout=1)
            return await original_commit(plan)

        monkeypatch.setattr(runtime, "_commit_plan", synchronized_commit)
        outcomes = await asyncio.gather(
            runtime.start_task(
                task.task_id,
                event_context=CTX,
                spawn_initiator_id="tool:start-a",
            ),
            runtime.start_task(
                task.task_id,
                event_context=CTX,
                spawn_initiator_id="tool:start-b",
            ),
            return_exceptions=True,
        )
        assert sum(not isinstance(item, BaseException) for item in outcomes) == 1
        [loser] = [item for item in outcomes if isinstance(item, BaseException)]
        assert isinstance(loser, EventWriteConflict)

    asyncio.run(run())
    started_events = [
        event
        for event in parent.event_log.iter()
        if isinstance(event, SubagentRunStartedEvent)
    ]
    assert len(started_events) == 1
    assert len(runtime.runs) == 1
    assert runtime.tasks[0].status == "running"


def test_completion_run_and_task_events_are_atomic_batch(tmp_path, monkeypatch) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    batches: list[tuple[type[AgentEvent], ...]] = []
    original_write_events = RuntimeSession.write_events

    async def recording_write_events(session, events, **kwargs):
        if session is parent:
            batches.append(tuple(type(event) for event in events))
        return await original_write_events(session, events, **kwargs)

    monkeypatch.setattr(RuntimeSession, "write_events", recording_write_events)

    async def run() -> None:
        task = await runtime.create_task(
            objective="Complete atomically",
            event_context=CTX,
            profile_id="review_worker",
        )
        child = await runtime.start_task(
            task.task_id,
            event_context=CTX,
            spawn_initiator_id="tool:start",
        )
        await runtime.complete_fake(
            child.subagent_run_id,
            summary="done",
            event_context=CTX,
        )

    asyncio.run(run())
    assert batches[-1] == (SubagentRunCompletedEvent, SubagentTaskCompletedEvent)


def test_tool_layer_never_reads_private_graph_dicts() -> None:
    source = inspect.getsource(CreateAgentTasksTool)
    source += inspect.getsource(WaitAgentTasksTool)
    source += inspect.getsource(StopAgentTaskTool)
    source += inspect.getsource(ListAgentsTool)
    for private_name in (
        "._tasks",
        "._runs",
        "._results",
        "._submitted_results",
        "._consumed_result_ids",
        "._delivered_result_ids",
    ):
        assert private_name not in source


def test_subagent_task_completion_updates_task_projection(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)

    async def run() -> None:
        task = await runtime.create_task(
            objective="Summarize completed work.",
            event_context=CTX,
            profile_id="research_worker",
        )
        subagent = await runtime.start_task(
            task.task_id, event_context=CTX, spawn_initiator_id="tool:create"
        )
        result = await runtime.complete_fake(
            subagent.subagent_run_id,
            summary="worker result",
            event_context=CTX,
        )

        assert runtime.tasks[0].status == "completed"
        assert runtime.tasks[0].result_id == result.result_id
        assert (
            runtime.result_for_run(runtime.runs[0].subagent_run_id).result_source
            == "inferred"
        )  # type: ignore[union-attr]
        assert any(
            isinstance(event, SubagentTaskCompletedEvent)
            for event in parent.event_log.iter()
        )
        graph = runtime.graph()
        assert graph.tasks[0].status == "completed"
        assert graph.tasks[0].result_id == result.result_id
        assert (
            graph.tasks[0].primary_result_artifact_id
            == result.final_message_artifact_id
        )

    asyncio.run(run())


def test_child_report_phase_updates_run_and_task_projection(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    tool = ListAgentsTool(runtime)

    async def run() -> None:
        task = await runtime.create_task(
            objective="Investigate a flaky test.",
            event_context=CTX,
            profile_id="research_worker",
            label="Research",
        )
        subagent = await runtime.start_task(
            task.task_id, event_context=CTX, spawn_initiator_id="tool:create"
        )

        await runtime.report_phase(
            subagent.subagent_run_id,
            phase="investigating",
            message="Reading nearby tests.",
            event_context=CTX,
            source_tool_call_id="tool:phase",
        )

        phase_event = next(
            event
            for event in parent.event_log.iter()
            if isinstance(event, SubagentPhaseReportedEvent)
        )
        assert phase_event.task_id == task.task_id
        assert phase_event.source_tool_call_id == "tool:phase"
        graph = runtime.graph()
        assert graph.nodes[0].phase == "investigating"
        assert graph.tasks[0].phase == "investigating"
        result = await tool.execute_async(
            ToolCall(id="tool:list", name="list_agents", arguments={}),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id, event_context=CTX
            ),
        )
        payload = json.loads(result.output)
        assert payload["items"][0]["phase"] == "investigating"

    asyncio.run(run())


def test_report_agent_result_submits_explicit_result_before_completion(
    tmp_path,
) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)

    async def run() -> None:
        subagent = await runtime.spawn_fake(task="child task", event_context=CTX)
        tool = ReportAgentResultTool(runtime, subagent.subagent_run_id)

        submitted = await tool.execute_async(
            ToolCall(
                id="tool:report-result",
                name="report_agent_result",
                arguments={
                    "summary": "explicit child result",
                    "output_preview": "explicit child result with evidence",
                },
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=runtime.child_runtime_session(
                    subagent.subagent_run_id
                ).runtime_session_id,
                event_context=CTX,
            ),
        )
        assert submitted.status is ToolResultState.SUCCESS
        submitted_event = next(
            event
            for event in parent.event_log.iter()
            if isinstance(event, SubagentResultSubmittedEvent)
        )
        assert submitted_event.result_source == "explicit"
        assert submitted_event.source_tool_call_id == "tool:report-result"
        assert not any(
            isinstance(event, SubagentRunCompletedEvent)
            for event in parent.event_log.iter()
        )

        result = await runtime.complete_fake(
            subagent.subagent_run_id,
            summary="explicit child result",
            event_context=CTX,
        )

        events = parent.event_log.iter()
        submitted_index = next(
            index
            for index, event in enumerate(events)
            if isinstance(event, SubagentResultSubmittedEvent)
        )
        completed_index = next(
            index
            for index, event in enumerate(events)
            if isinstance(event, SubagentRunCompletedEvent)
        )
        assert submitted_index < completed_index
        completed_event = events[completed_index]
        assert isinstance(completed_event, SubagentRunCompletedEvent)
        assert (
            completed_event.result_id == submitted_event.result_id == result.result_id
        )
        assert (
            runtime.result_for_run(runtime.runs[0].subagent_run_id).result_source
            == "explicit"
        )  # type: ignore[union-attr]
        waited = await runtime.wait_result(
            subagent.subagent_run_id,
            event_context=CTX,
            returned_to_tool_call_id="tool:wait",
        )
        assert waited.summary == "explicit child result"
        assert waited.result_source == "explicit"

    asyncio.run(run())


def test_production_explicit_completion_rejects_missing_native_child_ledger(
    tmp_path,
) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)

    async def run() -> None:
        child = await runtime.spawn_fake(task="child task", event_context=CTX)
        await runtime.submit_result(
            child.subagent_run_id,
            summary="forged explicit result",
            event_context=CTX,
            source_tool_call_id="tool:report-result",
        )
        with pytest.raises(Exception, match="exactly one child RunStart/RunEnd"):
            await runtime.complete_submitted_result(
                child.subagent_run_id,
                event_context=CTX,
            )
        assert not any(
            isinstance(event, SubagentRunCompletedEvent)
            for event in parent.event_log.iter()
        )

    asyncio.run(run())


def test_child_report_events_use_parent_spawn_context_not_child_native_context(
    tmp_path,
) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    child_context = EventContext(
        run_id="run:child-report-native",
        turn_id="turn:child-report-native",
        reply_id="reply:child-report-native",
    )

    async def run() -> None:
        subagent = await runtime.spawn_fake(task="child task", event_context=CTX)
        await runtime.report_phase(
            subagent.subagent_run_id,
            phase="working",
            event_context=child_context,
            source_tool_call_id="tool:child-phase",
        )
        await runtime.submit_result(
            subagent.subagent_run_id,
            summary="child explicit result",
            event_context=child_context,
            source_tool_call_id="tool:child-result",
        )

        graph_events = [
            event
            for event in parent.event_log.iter()
            if isinstance(
                event, (SubagentPhaseReportedEvent, SubagentResultSubmittedEvent)
            )
        ]
        assert len(graph_events) == 2
        assert all(event.run_id == CTX.run_id for event in graph_events)
        assert all(event.turn_id == CTX.turn_id for event in graph_events)
        assert all(event.reply_id == CTX.reply_id for event in graph_events)
        assert all(event.run_id != child_context.run_id for event in graph_events)

    asyncio.run(run())


def test_builtin_profiles_compute_child_tool_boundaries(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    parent.subagent_runtime = runtime
    executor = parent.create_tool_executor()
    exposure = preview_capability_plan(
        CapabilityRuntime(),
        workspace_root=tmp_path,
        workspace_kind="project",
        memory_domain=None,
        tool_registry=executor.registry,
        archive=parent.archive,
        runtime_session_id=parent.runtime_session_id,
        mcp_installation_id=parent.mcp_installation_id,
        user_input="profile test",
    )
    runtime.refresh_parent_capability_snapshot(
        exposure=exposure,
        permission_mode=PermissionMode.BYPASS_PERMISSIONS.value,
        permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS).to_dict(),
    )

    async def run() -> None:
        profiles = {}
        for profile_name in ("research_worker", "review_worker", "verification_worker"):
            task = await runtime.create_task(
                objective=f"{profile_name} task",
                event_context=CTX,
                profile_id=profile_name,
            )
            subagent = await runtime.start_task(
                task.task_id, event_context=CTX, spawn_initiator_id="tool:create"
            )
            profiles[profile_name] = set(subagent.capability_profile.allowed_tool_names)

        for profile_name in ("research_worker", "review_worker"):
            assert {"read_file", "search_files", "artifact_read"} <= profiles[
                profile_name
            ]
            assert {"report_agent_phase", "report_agent_result"} <= profiles[
                profile_name
            ]
            assert not (
                {"terminal", "terminal_process", "write_file", "edit_file"}
                & profiles[profile_name]
            )
            assert "spawn_agent" not in profiles[profile_name]
            assert not any(
                name.startswith("memory_") or name.startswith("remember_")
                for name in profiles[profile_name]
            )

        assert {
            "read_file",
            "search_files",
            "artifact_read",
            "terminal",
            "terminal_process",
        } <= profiles["verification_worker"]
        assert {"write_file", "edit_file", "spawn_agent"} & profiles[
            "verification_worker"
        ] == set()
        assert {"report_agent_phase", "report_agent_result"} <= profiles[
            "verification_worker"
        ]

    asyncio.run(run())


def test_create_agent_tasks_starts_independent_batch(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    parent.subagent_runtime = runtime
    executor = parent.create_tool_executor()
    exposure = preview_capability_plan(
        CapabilityRuntime(),
        workspace_root=tmp_path,
        workspace_kind="project",
        memory_domain=None,
        tool_registry=executor.registry,
        archive=parent.archive,
        runtime_session_id=parent.runtime_session_id,
        mcp_installation_id=parent.mcp_installation_id,
        user_input="create tasks",
    )
    runtime.refresh_parent_capability_snapshot(
        exposure=exposure,
        permission_mode=PermissionMode.BYPASS_PERMISSIONS.value,
        permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS).to_dict(),
    )
    tool = CreateAgentTasksTool(runtime)

    async def run() -> None:
        result = await tool.execute_async(
            ToolCall(
                id="tool:create-tasks",
                name="create_agent_tasks",
                arguments={
                    "tasks": [
                        {
                            "task_key": "review",
                            "label": "Review",
                            "profile": "review_worker",
                            "task": "Review the parser.",
                        },
                        {
                            "task_key": "verify",
                            "label": "Verify",
                            "profile": "verification_worker",
                            "task": "Run the smoke test.",
                        },
                    ]
                },
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id,
                event_context=CTX,
                context_id="context:create",
                model_call_index=5,
            ),
        )

        assert result.status is ToolResultState.SUCCESS
        payload = json.loads(result.output)
        assert payload["status"] == "accepted"
        assert payload["started_count"] == 2
        assert payload["batch_id"].startswith("subagent_batch:")
        assert [item["task_key"] for item in payload["tasks"]] == ["review", "verify"]
        assert len(runtime.tasks) == 2
        assert len(runtime.runs) == 2
        assert all(task.status == "running" for task in runtime.tasks)
        assert all(run.parent_context_id == "context:create" for run in runtime.runs)
        graph = runtime.graph()
        assert len(graph.tasks) == 2
        assert {task.task_key for task in graph.tasks} == {"review", "verify"}
        assert all(task.has_child_run for task in graph.tasks)

    asyncio.run(run())


def test_create_agent_tasks_materializes_batch_with_event_log_extend(
    tmp_path, monkeypatch
) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    tool = CreateAgentTasksTool(runtime)
    extend_calls = 0
    original_event_log = parent.event_log

    class BatchOnlyEventLog:
        def __getattr__(self, name):
            return getattr(original_event_log, name)

        def append(
            self,
            event,
            *,
            expected_last_sequence=None,
            deadline_monotonic=None,
        ):
            del event, expected_last_sequence, deadline_monotonic
            raise AssertionError(
                "create_agent_tasks must not append batch facts one by one"
            )

        def extend(
            self,
            events,
            *,
            expected_last_sequence=None,
            deadline_monotonic=None,
        ):
            nonlocal extend_calls
            extend_calls += 1
            return original_event_log.extend(
                events,
                expected_last_sequence=expected_last_sequence,
                deadline_monotonic=deadline_monotonic,
            )

        def iter(self, **kwargs):
            return original_event_log.iter(**kwargs)

        def next_sequence(self):
            return original_event_log.next_sequence()

    monkeypatch.setattr(parent, "event_log", BatchOnlyEventLog())

    async def run() -> None:
        result = await tool.execute_async(
            ToolCall(
                id="tool:create-tasks",
                name="create_agent_tasks",
                arguments={
                    "tasks": [
                        {
                            "task_key": "review",
                            "profile": "review_worker",
                            "task": "Review",
                        },
                        {
                            "task_key": "verify",
                            "profile": "verification_worker",
                            "task": "Verify",
                        },
                    ]
                },
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id, event_context=CTX
            ),
        )

        assert result.status is ToolResultState.SUCCESS
        assert extend_calls == 1
        payload = json.loads(result.output)
        assert payload["status"] == "accepted"
        assert payload["started_count"] == 2

    asyncio.run(run())


def test_create_agent_tasks_rejects_dependencies_without_persisting_tasks(
    tmp_path,
) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    tool = CreateAgentTasksTool(runtime)

    async def run() -> None:
        result = await tool.execute_async(
            ToolCall(
                id="tool:create-tasks",
                name="create_agent_tasks",
                arguments={
                    "tasks": [
                        {
                            "task_key": "review",
                            "profile": "review_worker",
                            "task": "Review the parser.",
                            "depends_on": ["other"],
                        }
                    ]
                },
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id, event_context=CTX
            ),
        )

        assert result.status is ToolResultState.ERROR
        payload = json.loads(result.output)
        assert payload["failed_stage"] == "preflight"
        assert payload["error_code"] == "subagent_task_batch_preflight_failed"
        assert payload["failed_task_keys"] == ["review"]
        assert not any(
            isinstance(event, SubagentTaskCreatedEvent)
            for event in parent.event_log.iter()
        )
        assert runtime.tasks == ()
        assert runtime.runs == ()

    asyncio.run(run())


def test_create_agent_tasks_dependency_waits_then_starts_after_upstream_completion(
    tmp_path,
) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    tool = CreateAgentTasksTool(runtime)

    async def run() -> None:
        created = await tool.execute_async(
            ToolCall(
                id="tool:create-tasks",
                name="create_agent_tasks",
                arguments={
                    "tasks": [
                        {
                            "task_key": "review",
                            "profile": "review_worker",
                            "task": "Review the parser.",
                        },
                        {
                            "task_key": "verify",
                            "profile": "verification_worker",
                            "task": "Verify the review.",
                            "depends_on": ["review"],
                        },
                    ]
                },
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id, event_context=CTX
            ),
        )
        payload = json.loads(created.output)
        review_task = next(
            item for item in payload["tasks"] if item["task_key"] == "review"
        )
        verify_task = next(
            item for item in payload["tasks"] if item["task_key"] == "verify"
        )
        assert review_task["status"] == "running"
        assert verify_task["status"] == "waiting_dependency"
        assert verify_task["subagent_run_id"] is None
        assert len(runtime.runs) == 1
        review_run_id = review_task["subagent_run_id"]

        await runtime.complete_fake(
            review_run_id, summary="review done", event_context=CTX
        )

        graph = runtime.graph()
        by_key = {task.task_key: task for task in graph.tasks}
        assert by_key["review"].status == "completed"
        assert by_key["verify"].status == "running"
        assert by_key["verify"].current_run_id is not None
        assert len(runtime.runs) == 2

    asyncio.run(run())


def test_dependency_start_unavailable_fails_task_and_blocks_downstream(
    tmp_path,
) -> None:
    parent, _locator, _child_logs, runtime = _runtime(
        tmp_path,
        budget=SubagentBudget(
            max_concurrent_children_per_parent_run=1,
            max_concurrent_children_per_host_session=1,
        ),
    )
    tool = CreateAgentTasksTool(runtime)

    async def run() -> None:
        created = await tool.execute_async(
            ToolCall(
                id="tool:create-capacity-failure",
                name="create_agent_tasks",
                arguments={
                    "tasks": [
                        {"task_key": "a", "profile": "review_worker", "task": "A"},
                        {
                            "task_key": "b",
                            "profile": "review_worker",
                            "task": "B",
                            "depends_on": ["a"],
                        },
                        {
                            "task_key": "c",
                            "profile": "review_worker",
                            "task": "C",
                            "depends_on": ["b"],
                        },
                    ]
                },
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id,
                event_context=CTX,
            ),
        )
        payload = json.loads(created.output)
        a_run_id = next(
            item["subagent_run_id"]
            for item in payload["tasks"]
            if item["task_key"] == "a"
        )
        reservation = runtime._execution_registry.reserve(  # noqa: SLF001 - simulate a concurrent command preflight.
            parent_run_id=CTX.run_id,
            count=1,
        )
        try:
            await runtime.complete_fake(a_run_id, summary="A done", event_context=CTX)
        finally:
            runtime._execution_registry.release_reservation(reservation)  # noqa: SLF001

    asyncio.run(run())
    tasks = {task.task_key: task for task in runtime.graph().tasks}
    assert tasks["a"].status == "completed"
    assert tasks["b"].status == "failed"
    assert tasks["c"].status == "blocked_dependency_failed"
    failed = next(
        event
        for event in parent.event_log.iter()
        if isinstance(event, SubagentTaskFailedEvent)
        and event.task_id == tasks["b"].task_id
    )
    assert failed.reason_code == "subagent_dependency_start_unavailable"
    assert tasks["c"].dependency_terminal_event_ids[tasks["b"].task_id] == failed.id


def test_dependency_post_commit_child_start_failure_keeps_upstream_completed(
    tmp_path,
    monkeypatch,
) -> None:
    _parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    tool = CreateAgentTasksTool(runtime)

    async def run() -> None:
        created = await tool.execute_async(
            ToolCall(
                id="tool:create-postcommit-dependency-failure",
                name="create_agent_tasks",
                arguments={
                    "tasks": [
                        {"task_key": "a", "profile": "review_worker", "task": "A"},
                        {
                            "task_key": "b",
                            "profile": "review_worker",
                            "task": "B",
                            "depends_on": ["a"],
                        },
                        {
                            "task_key": "c",
                            "profile": "review_worker",
                            "task": "C",
                            "depends_on": ["b"],
                        },
                    ]
                },
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=runtime.parent_runtime_session.runtime_session_id,
                event_context=CTX,
            ),
        )
        payload = json.loads(created.output)
        a_run_id = next(
            item["subagent_run_id"]
            for item in payload["tasks"]
            if item["task_key"] == "a"
        )

        def fail_child_session(**_kwargs):
            raise OSError("synthetic child adapter failure")

        monkeypatch.setattr(
            runtime, "_create_child_runtime_session", fail_child_session
        )
        result = await runtime.complete_fake(
            a_run_id, summary="A completed", event_context=CTX
        )

        assert result.summary == "A completed"
        by_key = {task.task_key: task for task in runtime.graph().tasks}
        assert by_key["a"].status == "completed"
        assert by_key["b"].status == "failed"
        assert by_key["c"].status == "blocked_dependency_failed"

    asyncio.run(run())


def test_create_agent_tasks_post_commit_failure_terminalizes_materialized_batch(
    tmp_path, monkeypatch
) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    tool = CreateAgentTasksTool(runtime)
    original_materialize_task_batch = runtime.materialize_task_batch

    async def flaky_materialize_task_batch(*args, **kwargs):
        await original_materialize_task_batch(*args, **kwargs)
        raise RuntimeError("synthetic child start failure")

    monkeypatch.setattr(runtime, "materialize_task_batch", flaky_materialize_task_batch)

    async def run() -> None:
        result = await tool.execute_async(
            ToolCall(
                id="tool:create-tasks",
                name="create_agent_tasks",
                arguments={
                    "tasks": [
                        {"task_key": "a", "profile": "review_worker", "task": "A"},
                        {"task_key": "b", "profile": "review_worker", "task": "B"},
                        {"task_key": "c", "profile": "review_worker", "task": "C"},
                    ]
                },
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id, event_context=CTX
            ),
        )

        assert result.status is ToolResultState.ERROR
        payload = json.loads(result.output)
        assert payload["failed_stage"] == "post_commit_start"
        assert payload["error_code"] == "subagent_task_batch_start_failed"
        assert "tasks" not in payload

        graph = runtime.graph()
        assert {task.status for task in graph.tasks} == {"cancelled"}
        assert all(
            not task.current_run_id or task.status == "cancelled"
            for task in graph.tasks
        )
        assert {run.status for run in runtime.runs} == {"cancelled"}
        task_cancelled_events = [
            event
            for event in parent.event_log.iter()
            if isinstance(event, SubagentTaskCancelledEvent)
        ]
        assert len(task_cancelled_events) == 3
        assert len({event.repair_id for event in task_cancelled_events}) == 1
        assert task_cancelled_events[0].repair_id is not None

    asyncio.run(run())


def test_materialized_batch_start_failure_commits_repair_before_bounded_drain(
    tmp_path,
    monkeypatch,
) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    tool = CreateAgentTasksTool(runtime)
    observed_terminal_before_drain: list[bool] = []

    def fail_child_session(**_kwargs):
        raise OSError("synthetic child start failure")

    async def fail_first_drain(run_ids, *, timeout_seconds):
        del timeout_seconds
        if not run_ids:
            return
        graph = runtime.graph()
        observed_terminal_before_drain.append(
            {run.status for run in runtime.runs} == {"cancelled"}
            and {task.status for task in graph.tasks} == {"cancelled"}
        )
        raise TimeoutError("synthetic stubborn child cleanup")

    monkeypatch.setattr(runtime, "_create_child_runtime_session", fail_child_session)
    monkeypatch.setattr(runtime._execution_registry, "drain_run_ids", fail_first_drain)  # noqa: SLF001

    async def run() -> None:
        result = await tool.execute_async(
            ToolCall(
                id="tool:create-repair-before-drain",
                name="create_agent_tasks",
                arguments={
                    "tasks": [
                        {
                            "task_key": "a",
                            "profile": "review_worker",
                            "task": "A",
                        }
                    ]
                },
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id,
                event_context=CTX,
            ),
        )
        assert result.status is ToolResultState.ERROR
        assert observed_terminal_before_drain == [True]
        assert {run.status for run in runtime.runs} == {"cancelled"}
        assert {task.status for task in runtime.tasks} == {"cancelled"}

    asyncio.run(run())


def test_start_task_failure_commits_terminal_facts_before_child_drain(
    tmp_path,
    monkeypatch,
) -> None:
    _parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    task = asyncio.run(
        runtime.create_task(
            objective="A",
            event_context=CTX,
            profile_id="review_worker",
            task_key="a",
        )
    )
    observed_terminal_before_drain: list[bool] = []

    def fail_child_session(**_kwargs):
        raise OSError("synthetic child start failure")

    async def fail_drain(run_id, *, timeout_seconds=None):
        del timeout_seconds
        observed_terminal_before_drain.append(
            runtime._require_run(run_id).status == "failed"  # noqa: SLF001
            and runtime._require_task(task.task_id).status == "failed"  # noqa: SLF001
        )
        raise TimeoutError("synthetic stubborn child cleanup")

    monkeypatch.setattr(runtime, "_create_child_runtime_session", fail_child_session)
    monkeypatch.setattr(runtime._execution_registry, "cancel", fail_drain)  # noqa: SLF001

    async def run() -> None:
        with pytest.raises(TimeoutError, match="stubborn child cleanup"):
            await runtime.start_task(
                task.task_id,
                event_context=CTX,
                spawn_initiator_id="tool:create",
            )
        assert observed_terminal_before_drain == [True]
        assert runtime._require_task(task.task_id).status == "failed"  # noqa: SLF001

    asyncio.run(run())


def test_create_agent_tasks_post_commit_failure_repairs_waiting_and_blocked_batch(
    tmp_path,
    monkeypatch,
) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    upstream = asyncio.run(
        runtime.create_task(
            objective="Failed upstream",
            event_context=CTX,
            profile_id="review_worker",
            task_key="upstream",
        )
    )
    asyncio.run(
        runtime.cancel_materialized_task(
            upstream.task_id,
            event_context=CTX,
            reason_code="upstream_cancelled",
        )
    )
    tool = CreateAgentTasksTool(runtime)
    original_materialize = runtime.materialize_task_batch

    async def fail_after_materialization(*args, **kwargs):
        await original_materialize(*args, **kwargs)
        raise RuntimeError("synthetic post-commit failure")

    monkeypatch.setattr(runtime, "materialize_task_batch", fail_after_materialization)

    async def run() -> None:
        result = await tool.execute_async(
            ToolCall(
                id="tool:create-repair-mixed",
                name="create_agent_tasks",
                arguments={
                    "tasks": [
                        {"task_key": "a", "profile": "review_worker", "task": "A"},
                        {
                            "task_key": "b",
                            "profile": "review_worker",
                            "task": "B",
                            "depends_on": ["a"],
                        },
                        {
                            "task_key": "c",
                            "profile": "review_worker",
                            "task": "C",
                            "depends_on": [f"task:{upstream.task_id}"],
                        },
                    ]
                },
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id,
                event_context=CTX,
            ),
        )

        assert result.status is ToolResultState.ERROR
        payload = json.loads(result.output)
        assert payload["failed_stage"] == "post_commit_start"
        repaired_tasks = [
            task for task in runtime.tasks if task.batch_id == payload["batch_id"]
        ]
        assert len(repaired_tasks) == 3
        assert {task.status for task in repaired_tasks} == {"cancelled"}
        repair_events = [
            event
            for event in parent.event_log.iter()
            if isinstance(event, SubagentTaskCancelledEvent)
            and event.batch_id == payload["batch_id"]
        ]
        assert len(repair_events) == 3
        assert len({event.repair_id for event in repair_events}) == 1
        assert repair_events[0].repair_id is not None

    asyncio.run(run())


def test_dependency_failure_blocks_downstream_task_without_retry(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    tool = CreateAgentTasksTool(runtime)

    async def run() -> None:
        created = await tool.execute_async(
            ToolCall(
                id="tool:create-tasks",
                name="create_agent_tasks",
                arguments={
                    "tasks": [
                        {"task_key": "a", "profile": "review_worker", "task": "A"},
                        {
                            "task_key": "b",
                            "profile": "review_worker",
                            "task": "B",
                            "depends_on": ["a"],
                        },
                    ]
                },
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id, event_context=CTX
            ),
        )
        payload = json.loads(created.output)
        a_run_id = next(item for item in payload["tasks"] if item["task_key"] == "a")[
            "subagent_run_id"
        ]

        await runtime.fail(
            a_run_id,
            reason_code="test_failure",
            reason_message="A failed",
            event_context=CTX,
        )

        graph = runtime.graph()
        by_key = {task.task_key: task for task in graph.tasks}
        assert by_key["a"].status == "failed"
        assert by_key["b"].status == "blocked_dependency_failed"
        assert by_key["b"].current_run_id is None
        assert by_key["b"].blocked_by_task_ids == (by_key["a"].task_id,)
        assert by_key["b"].dependency_status_snapshot == {by_key["a"].task_id: "failed"}
        failed_event = next(
            event
            for event in parent.event_log.iter()
            if isinstance(event, SubagentTaskFailedEvent)
            and event.task_id == by_key["a"].task_id
        )
        assert (
            by_key["b"].dependency_terminal_event_ids[by_key["a"].task_id]
            == failed_event.id
        )
        assert len(runtime.runs) == 1

    asyncio.run(run())


def test_dependency_failure_propagates_transitively(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    tool = CreateAgentTasksTool(runtime)

    async def run() -> None:
        created = await tool.execute_async(
            ToolCall(
                id="tool:create-tasks",
                name="create_agent_tasks",
                arguments={
                    "tasks": [
                        {"task_key": "a", "profile": "review_worker", "task": "A"},
                        {
                            "task_key": "b",
                            "profile": "review_worker",
                            "task": "B",
                            "depends_on": ["a"],
                        },
                        {
                            "task_key": "c",
                            "profile": "review_worker",
                            "task": "C",
                            "depends_on": ["b"],
                        },
                    ]
                },
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id, event_context=CTX
            ),
        )
        payload = json.loads(created.output)
        a_run_id = next(item for item in payload["tasks"] if item["task_key"] == "a")[
            "subagent_run_id"
        ]

        await runtime.fail(
            a_run_id,
            reason_code="test_failure",
            reason_message="A failed",
            event_context=CTX,
        )

        graph = runtime.graph()
        by_key = {task.task_key: task for task in graph.tasks}
        assert by_key["a"].status == "failed"
        assert by_key["b"].status == "blocked_dependency_failed"
        assert by_key["c"].status == "blocked_dependency_failed"
        assert by_key["c"].blocked_by_task_ids == (by_key["b"].task_id,)
        assert by_key["c"].dependency_status_snapshot == {
            by_key["b"].task_id: "blocked_dependency_failed"
        }
        blocked_b_event = next(
            event
            for event in parent.event_log.iter()
            if isinstance(event, SubagentTaskBlockedEvent)
            and event.task_id == by_key["b"].task_id
            and event.status == "blocked_dependency_failed"
        )
        assert (
            by_key["c"].dependency_terminal_event_ids[by_key["b"].task_id]
            == blocked_b_event.id
        )

        waited = await runtime.wait_tasks(
            (by_key["a"].task_id, by_key["b"].task_id, by_key["c"].task_id),
            event_context=CTX,
            consumer_tool_call_id="tool:wait-tasks",
            settle="all",
            timeout_seconds=0,
        )
        assert {item["status"] for item in waited} == {
            "failed",
            "blocked_dependency_failed",
        }
        assert len(waited) == 3

    asyncio.run(run())


def test_create_agent_tasks_blocks_transitive_dependency_on_existing_failed_task(
    tmp_path,
) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    tool = CreateAgentTasksTool(runtime)

    async def run() -> None:
        upstream = await runtime.create_task(
            objective="Already failed upstream.",
            event_context=CTX,
            profile_id="review_worker",
            task_key="upstream",
        )
        await runtime.cancel_materialized_task(
            upstream.task_id,
            event_context=CTX,
            reason_code="synthetic_upstream_cancelled",
        )

        result = await tool.execute_async(
            ToolCall(
                id="tool:create-tasks",
                name="create_agent_tasks",
                arguments={
                    "tasks": [
                        {
                            "task_key": "b",
                            "profile": "review_worker",
                            "task": "B",
                            "depends_on": [f"task:{upstream.task_id}"],
                        },
                        {
                            "task_key": "c",
                            "profile": "review_worker",
                            "task": "C",
                            "depends_on": ["b"],
                        },
                    ]
                },
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id, event_context=CTX
            ),
        )

        assert result.status is ToolResultState.SUCCESS
        payload = json.loads(result.output)
        assert payload["status"] == "accepted"
        assert payload["started_count"] == 0
        graph = runtime.graph()
        by_key = {task.task_key: task for task in graph.tasks}
        assert by_key["b"].status == "blocked_dependency_failed"
        assert by_key["c"].status == "blocked_dependency_failed"
        assert by_key["b"].blocked_by_task_ids == (upstream.task_id,)
        upstream_cancelled_event = next(
            event
            for event in parent.event_log.iter()
            if isinstance(event, SubagentTaskCancelledEvent)
            and event.task_id == upstream.task_id
        )
        assert (
            by_key["b"].dependency_terminal_event_ids[upstream.task_id]
            == upstream_cancelled_event.id
        )
        assert by_key["c"].blocked_by_task_ids == (by_key["b"].task_id,)
        assert by_key["c"].dependency_status_snapshot == {
            by_key["b"].task_id: "blocked_dependency_failed"
        }
        blocked_b_event = next(
            event
            for event in parent.event_log.iter()
            if isinstance(event, SubagentTaskBlockedEvent)
            and event.task_id == by_key["b"].task_id
            and event.status == "blocked_dependency_failed"
        )
        assert (
            by_key["c"].dependency_terminal_event_ids[by_key["b"].task_id]
            == blocked_b_event.id
        )
        assert by_key["c"].dependency_generation is not None

    asyncio.run(run())


def test_create_agent_tasks_rejects_dependency_cycle_without_persisting_tasks(
    tmp_path,
) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    tool = CreateAgentTasksTool(runtime)

    async def run() -> None:
        result = await tool.execute_async(
            ToolCall(
                id="tool:create-tasks",
                name="create_agent_tasks",
                arguments={
                    "tasks": [
                        {
                            "task_key": "a",
                            "profile": "review_worker",
                            "task": "A",
                            "depends_on": ["b"],
                        },
                        {
                            "task_key": "b",
                            "profile": "review_worker",
                            "task": "B",
                            "depends_on": ["a"],
                        },
                    ]
                },
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id, event_context=CTX
            ),
        )

        assert result.status is ToolResultState.ERROR
        payload = json.loads(result.output)
        assert payload["failed_stage"] == "preflight"
        assert "dependency cycle" in payload["diagnostics"][0]["message"]
        assert runtime.tasks == ()
        assert not any(
            isinstance(event, SubagentTaskCreatedEvent)
            for event in parent.event_log.iter()
        )

    asyncio.run(run())


def test_wait_agent_tasks_consumes_completed_task_results(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    wait_tool = WaitAgentTasksTool(runtime)

    async def run() -> None:
        tasks = []
        for key in ("a", "b"):
            task = await runtime.create_task(
                objective=f"Task {key}",
                event_context=CTX,
                profile_id="review_worker",
                task_key=key,
            )
            subagent = await runtime.start_task(
                task.task_id, event_context=CTX, spawn_initiator_id="tool:create"
            )
            await runtime.complete_fake(
                subagent.subagent_run_id, summary=f"result {key}", event_context=CTX
            )
            tasks.append(task)

        result = await wait_tool.execute_async(
            ToolCall(
                id="tool:wait-tasks",
                name="wait_agent_tasks",
                arguments={
                    "task_ids": [task.task_id for task in tasks],
                    "settle": "all",
                },
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id, event_context=CTX
            ),
        )

        assert result.status is ToolResultState.SUCCESS
        payload = json.loads(result.output)
        assert payload["returned_count"] == 2
        assert {item["summary"] for item in payload["results"]} == {
            "result a",
            "result b",
        }
        consumed = [
            event
            for event in parent.event_log.iter()
            if isinstance(event, SubagentResultConsumedEvent)
        ]
        assert len(consumed) == 2
        assert {event.kind for event in consumed} == {"wait_task"}
        graph = runtime.graph()
        assert all(task.consumed_by_wait for task in graph.tasks)

    asyncio.run(run())


def test_wait_agent_serializes_nested_explicit_result_diagnostics(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    tool = WaitAgentTool(runtime)

    async def run() -> None:
        child = await runtime.spawn_fake(task="diagnostic child", event_context=CTX)
        await runtime.submit_result(
            child.subagent_run_id,
            summary="done",
            event_context=CTX,
            diagnostics=({"outer": {"inner": ["value"]}},),
            source_tool_call_id="tool:explicit-result",
        )
        await runtime.complete_fake(
            child.subagent_run_id,
            summary="done",
            event_context=CTX,
        )

        result = await tool.execute_async(
            ToolCall(
                id="tool:wait-nested-diagnostic",
                name="wait_agent",
                arguments={"subagent_run_id": child.subagent_run_id},
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id,
                event_context=CTX,
            ),
        )
        payload = json.loads(result.output)
        assert payload["diagnostics"] == [{"outer": {"inner": ["value"]}}]

    asyncio.run(run())


def test_wait_agent_tasks_consumes_resultless_cancelled_terminal_fact(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)

    async def run() -> None:
        task = await runtime.create_task(
            objective="Cancelled before start",
            event_context=CTX,
            profile_id="review_worker",
            task_key="cancelled",
        )
        await runtime.cancel_materialized_task(
            task.task_id,
            event_context=CTX,
            reason_code="no_longer_needed",
        )

        payloads = await runtime.wait_tasks(
            (task.task_id,),
            event_context=CTX,
            consumer_tool_call_id="tool:wait-cancelled",
            timeout_seconds=0,
        )

        assert payloads == (
            {
                "task_id": task.task_id,
                "task_key": "cancelled",
                "status": "cancelled",
                "subagent_run_id": None,
                "child_runtime_session_id": None,
                "result_id": None,
                "summary": None,
                "output_preview": None,
                "result_artifact_id": None,
                "artifact_ids": [],
                "result_source": "none",
                "consumed": False,
            },
        )
        terminal = next(
            event
            for event in parent.event_log.iter()
            if isinstance(event, SubagentTaskCancelledEvent)
            and event.task_id == task.task_id
        )
        consumed = next(
            event
            for event in parent.event_log.iter()
            if isinstance(event, SubagentResultConsumedEvent)
            and event.task_id == task.task_id
        )
        assert consumed.result_id is None
        assert consumed.consumed_status == "cancelled"
        assert consumed.terminal_event_id == terminal.id

    asyncio.run(run())


def test_wait_agent_tasks_first_does_not_cancel_unsettled_tasks(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    wait_tool = WaitAgentTasksTool(runtime)

    async def run() -> None:
        done_task = await runtime.create_task(
            objective="Done task",
            event_context=CTX,
            profile_id="review_worker",
            task_key="done",
        )
        done_run = await runtime.start_task(
            done_task.task_id, event_context=CTX, spawn_initiator_id="tool:create"
        )
        await runtime.complete_fake(
            done_run.subagent_run_id, summary="done result", event_context=CTX
        )
        running_task = await runtime.create_task(
            objective="Running task",
            event_context=CTX,
            profile_id="review_worker",
            task_key="running",
        )
        running_run = await runtime.start_task(
            running_task.task_id, event_context=CTX, spawn_initiator_id="tool:create"
        )

        result = await wait_tool.execute_async(
            ToolCall(
                id="tool:wait-first",
                name="wait_agent_tasks",
                arguments={
                    "task_ids": [done_task.task_id, running_task.task_id],
                    "settle": "first",
                },
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id, event_context=CTX
            ),
        )

        payload = json.loads(result.output)
        assert payload["returned_count"] == 1
        assert payload["results"][0]["task_id"] == done_task.task_id
        assert (
            next(
                run
                for run in runtime.runs
                if run.subagent_run_id == running_run.subagent_run_id
            ).status
            == "running"
        )
        assert not any(
            isinstance(event, SubagentRunCancelledEvent)
            and event.subagent_run_id == running_run.subagent_run_id
            for event in parent.event_log.iter()
        )

    asyncio.run(run())


def test_wait_agent_tasks_timeout_returns_partial_without_cancelling_unsettled(
    tmp_path,
) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    wait_tool = WaitAgentTasksTool(runtime)

    async def run() -> None:
        done_task = await runtime.create_task(
            objective="Done before timeout",
            event_context=CTX,
            profile_id="review_worker",
            task_key="done-timeout",
        )
        done_run = await runtime.start_task(
            done_task.task_id,
            event_context=CTX,
            spawn_initiator_id="tool:create",
        )
        await runtime.complete_fake(
            done_run.subagent_run_id, summary="partial result", event_context=CTX
        )
        running_task = await runtime.create_task(
            objective="Still running at timeout",
            event_context=CTX,
            profile_id="review_worker",
            task_key="running-timeout",
        )
        running_run = await runtime.start_task(
            running_task.task_id,
            event_context=CTX,
            spawn_initiator_id="tool:create",
        )

        result = await wait_tool.execute_async(
            ToolCall(
                id="tool:wait-timeout",
                name="wait_agent_tasks",
                arguments={
                    "task_ids": [done_task.task_id, running_task.task_id],
                    "settle": "all",
                    "timeout_seconds": 0,
                },
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id,
                event_context=CTX,
            ),
        )

        payload = json.loads(result.output)
        assert payload["returned_count"] == 1
        assert payload["results"][0]["task_id"] == done_task.task_id
        assert (
            next(
                run
                for run in runtime.runs
                if run.subagent_run_id == running_run.subagent_run_id
            ).status
            == "running"
        )
        assert not any(
            isinstance(event, SubagentRunCancelledEvent)
            and event.subagent_run_id == running_run.subagent_run_id
            for event in parent.event_log.iter()
        )

    asyncio.run(run())


def test_wait_agent_tasks_repeated_wait_requires_include_consumed(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)

    async def run() -> None:
        task = await runtime.create_task(
            objective="Consume exactly when requested",
            event_context=CTX,
            profile_id="review_worker",
            task_key="consumed",
        )
        child = await runtime.start_task(
            task.task_id,
            event_context=CTX,
            spawn_initiator_id="tool:create",
        )
        await runtime.complete_fake(
            child.subagent_run_id, summary="consumable", event_context=CTX
        )

        first = await runtime.wait_tasks(
            (task.task_id,),
            event_context=CTX,
            consumer_tool_call_id="tool:wait:first",
            timeout_seconds=0,
        )
        hidden = await runtime.wait_tasks(
            (task.task_id,),
            event_context=CTX,
            consumer_tool_call_id="tool:wait:hidden",
            timeout_seconds=0,
        )
        included = await runtime.wait_tasks(
            (task.task_id,),
            event_context=CTX,
            consumer_tool_call_id="tool:wait:included",
            timeout_seconds=0,
            include_consumed=True,
        )

        assert first[0]["consumed"] is False
        assert hidden == ()
        assert included[0]["consumed"] is True
        consumed_events = [
            event
            for event in parent.event_log.iter()
            if isinstance(event, SubagentResultConsumedEvent)
            and event.task_id == task.task_id
        ]
        assert [event.consumer_tool_call_id for event in consumed_events] == [
            "tool:wait:first",
            "tool:wait:included",
        ]

    asyncio.run(run())


def test_stop_agent_task_cancels_active_attempt_and_task_projection(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    stop_tool = StopAgentTaskTool(runtime)

    async def run() -> None:
        task = await runtime.create_task(
            objective="Long task",
            event_context=CTX,
            profile_id="verification_worker",
            task_key="long",
        )
        subagent = await runtime.start_task(
            task.task_id, event_context=CTX, spawn_initiator_id="tool:create"
        )

        result = await stop_tool.execute_async(
            ToolCall(
                id="tool:stop-task",
                name="stop_agent_task",
                arguments={"task_id": task.task_id, "reason": "No longer needed."},
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id, event_context=CTX
            ),
        )

        assert result.status is ToolResultState.SUCCESS
        payload = json.loads(result.output)
        assert payload["status"] == "cancelled"
        assert runtime.runs[0].status == "cancelled"
        assert runtime.tasks[0].status == "cancelled"
        assert any(
            isinstance(event, SubagentTaskCancelledEvent)
            and event.task_id == task.task_id
            for event in parent.event_log.iter()
        )
        graph = runtime.graph()
        assert graph.tasks[0].status == "cancelled"
        assert graph.tasks[0].current_run_id == subagent.subagent_run_id

    asyncio.run(run())


def test_runtime_bootstraps_completed_result_from_parent_event_log(tmp_path) -> None:
    parent, locator, child_logs, runtime = _runtime(tmp_path)

    async def seed() -> None:
        subagent = await runtime.spawn_fake(task="child task", event_context=CTX)
        await runtime.complete_fake(
            subagent.subagent_run_id, summary="bootstrapped result", event_context=CTX
        )

    asyncio.run(seed())
    _resumed_parent, resumed = _resumed_runtime(parent, locator, child_logs)

    async def run() -> None:
        [bootstrapped] = resumed.runs
        wait_context = EventContext(
            run_id="run:wait", turn_id="turn:wait", reply_id="reply:wait"
        )
        await _emit_parent_run_start(
            resumed.parent_runtime_session,
            event_context=wait_context,
        )
        result = await resumed.wait_result(
            bootstrapped.subagent_run_id,
            event_context=wait_context,
            returned_to_tool_call_id="tool:wait",
        )

        assert result.summary == "bootstrapped result"
        assert resumed.graph().nodes[0].consumed_by_wait is True

    asyncio.run(run())


def test_restart_preserves_waiting_dependency_fact(tmp_path) -> None:
    parent, locator, child_logs, runtime = _runtime(tmp_path)
    tool = CreateAgentTasksTool(runtime)

    async def seed() -> None:
        result = await tool.execute_async(
            ToolCall(
                id="tool:restart-waiting",
                name="create_agent_tasks",
                arguments={
                    "tasks": [
                        {"task_key": "a", "profile": "review_worker", "task": "A"},
                        {
                            "task_key": "b",
                            "profile": "review_worker",
                            "task": "B",
                            "depends_on": ["a"],
                        },
                    ]
                },
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id,
                event_context=CTX,
            ),
        )
        assert result.status is ToolResultState.SUCCESS

    asyncio.run(seed())
    _resumed_parent, resumed = _resumed_runtime(parent, locator, child_logs)
    before = {task.task_key: task for task in runtime.graph().tasks}
    after = {task.task_key: task for task in resumed.graph().tasks}
    assert before["b"] == after["b"]
    assert after["b"].status == "waiting_dependency"


def test_restart_preserves_blocked_dependency_terminal_refs_and_generation(
    tmp_path,
) -> None:
    parent, locator, child_logs, runtime = _runtime(tmp_path)
    tool = CreateAgentTasksTool(runtime)

    async def seed() -> None:
        result = await tool.execute_async(
            ToolCall(
                id="tool:restart-blocked",
                name="create_agent_tasks",
                arguments={
                    "tasks": [
                        {"task_key": "a", "profile": "review_worker", "task": "A"},
                        {
                            "task_key": "b",
                            "profile": "review_worker",
                            "task": "B",
                            "depends_on": ["a"],
                        },
                    ]
                },
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id,
                event_context=CTX,
            ),
        )
        payload = json.loads(result.output)
        run_id = next(
            item["subagent_run_id"]
            for item in payload["tasks"]
            if item["task_key"] == "a"
        )
        await runtime.fail(run_id, reason_code="restart_failure", event_context=CTX)

    asyncio.run(seed())
    blocked_before = next(
        task for task in runtime.graph().tasks if task.task_key == "b"
    )
    _resumed_parent, resumed = _resumed_runtime(parent, locator, child_logs)
    blocked_after = next(task for task in resumed.graph().tasks if task.task_key == "b")
    assert (
        blocked_after.dependency_terminal_event_ids
        == blocked_before.dependency_terminal_event_ids
    )
    assert blocked_after.dependency_generation == blocked_before.dependency_generation
    assert blocked_after.dependency_generation is not None


def test_restart_preserves_run_budget_after_default_config_change(tmp_path) -> None:
    original_budget = SubagentBudget(
        max_concurrent_children_per_parent_run=2,
        max_concurrent_children_per_host_session=3,
        max_total_child_runs_per_parent_run=5,
        max_result_summary_chars_per_child=777,
        max_subagent_results_per_parent_compile=2,
    )
    parent, locator, child_logs, runtime = _runtime(tmp_path, budget=original_budget)

    async def seed() -> None:
        await runtime.spawn_fake(task="frozen budget", event_context=CTX)

    asyncio.run(seed())
    _resumed_parent, resumed = _resumed_runtime(parent, locator, child_logs)
    [run] = resumed.runs
    assert run.budget == original_budget
    assert resumed.default_budget != original_budget


def test_pending_results_zero_cap_selects_nothing(tmp_path) -> None:
    _parent, _locator, _child_logs, runtime = _runtime(tmp_path)

    async def seed() -> None:
        run = await runtime.spawn_fake(task="zero cap", event_context=CTX)
        await runtime.complete_fake(
            run.subagent_run_id,
            summary="must remain pending",
            event_context=CTX,
        )

    asyncio.run(seed())

    assert runtime.pending_result_delivery_count() == 1
    assert runtime.pending_results_for_delivery(max_results=0) == ()


def test_restart_preserves_consumed_and_delivered_sets_from_facts(tmp_path) -> None:
    parent, locator, child_logs, runtime = _runtime(tmp_path)

    async def seed() -> tuple[str, str]:
        waited_run = await runtime.spawn_fake(task="waited", event_context=CTX)
        waited_result = await runtime.complete_fake(
            waited_run.subagent_run_id,
            summary="waited result",
            event_context=CTX,
        )
        await runtime.wait_result(
            waited_run.subagent_run_id,
            event_context=CTX,
            returned_to_tool_call_id="tool:wait-restart",
        )
        delivered_run = await runtime.spawn_fake(task="delivered", event_context=CTX)
        delivered_result = await runtime.complete_fake(
            delivered_run.subagent_run_id,
            summary="delivered result",
            event_context=CTX,
        )
        await parent.write_event(
            ModelCallStartEvent(
                **CTX.event_fields(),
                **model_call_start_fields(
                    context_id="context:restart-delivery",
                    model_call_index=3,
                ),
            )
        )
        await runtime.mark_results_delivered(
            (delivered_result,),
            event_context=CTX,
            context_id="context:restart-delivery",
            model_call_index=3,
            section_id="subagent:results",
        )
        return waited_result.result_id, delivered_result.result_id

    waited_result_id, delivered_result_id = asyncio.run(seed())
    _resumed_parent, resumed = _resumed_runtime(parent, locator, child_logs)
    graph = resumed.graph()
    by_result = {node.result_id: node for node in graph.nodes}
    assert by_result[waited_result_id].consumed_by_wait is True
    assert by_result[waited_result_id].delivered is False
    assert by_result[delivered_result_id].delivered is True
    assert by_result[delivered_result_id].consumed_by_wait is False


def test_restart_does_not_require_archive_for_graph_equality(tmp_path) -> None:
    parent, locator, child_logs, runtime = _runtime(tmp_path)

    async def seed() -> None:
        child = await runtime.spawn_fake(task="artifact-backed task", event_context=CTX)
        await runtime.complete_fake(
            child.subagent_run_id, summary="done", event_context=CTX
        )

    asyncio.run(seed())
    expected = runtime.graph()
    resumed_parent = RuntimeSession(
        parent.workspace_root,
        event_log=parent.event_log,
        archive=InMemoryArchiveStore(),
        tool_result_artifacts=parent.tool_result_artifacts,
        runtime_session_id=parent.runtime_session_id,
    )
    resumed = SubagentRuntime(
        parent_runtime_session=resumed_parent,
        child_event_log_factory=lambda runtime_session_id: child_logs.setdefault(
            runtime_session_id,
            InMemoryEventLog(),
        ),
        event_log_locator=locator,
    )
    assert resumed.graph() == expected


def test_subagent_spawn_caps_are_enforced(tmp_path) -> None:
    _parent, _locator, _child_logs, runtime = _runtime(
        tmp_path,
        budget=SubagentBudget(
            max_concurrent_children_per_parent_run=1,
            max_concurrent_children_per_host_session=1,
        ),
    )

    async def run() -> None:
        await runtime.spawn_fake(task="first", event_context=CTX)
        with pytest.raises(
            SubagentLimitExceeded, match="max_concurrent_children_per_parent_run"
        ):
            await runtime.spawn_fake(task="second", event_context=CTX)

    asyncio.run(run())


def test_closing_child_handle_continues_to_occupy_concurrency_capacity(
    tmp_path,
) -> None:
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    budget = SubagentBudget(
        max_concurrent_children_per_parent_run=1,
        max_concurrent_children_per_host_session=1,
    )

    async def stubborn_child(_runtime: SubagentRuntime, _run) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_started.set()
            await allow_cleanup.wait()

    _parent, _locator, _child_logs, runtime = _runtime(
        tmp_path,
        budget=budget,
        child_runner=stubborn_child,
    )

    async def run() -> None:
        child = await runtime.spawn_agent(task="stubborn", event_context=CTX)
        await started.wait()
        with pytest.raises(TimeoutError, match="Timed out draining child coroutine"):
            await runtime.cancel(
                child.subagent_run_id,
                event_context=CTX,
                drain_timeout_seconds=0.01,
            )
        await cleanup_started.wait()

        assert runtime._execution_registry.get(child.subagent_run_id) is not None  # noqa: SLF001
        with pytest.raises(
            SubagentLimitExceeded,
            match="max_concurrent_children_per_parent_run",
        ):
            runtime.validate_can_start_batch(CTX.run_id, count=1, budget=budget)

        allow_cleanup.set()
        handle = runtime._execution_registry.get(child.subagent_run_id)  # noqa: SLF001
        assert handle is not None and handle.coroutine is not None
        await handle.coroutine
        await asyncio.sleep(0)
        runtime.validate_can_start_batch(CTX.run_id, count=1, budget=budget)

    asyncio.run(run())


def test_restart_active_run_without_handle_repairs_fail_closed(tmp_path) -> None:
    parent, locator, child_logs, runtime = _runtime(tmp_path)

    async def seed() -> None:
        await runtime.spawn_fake(task="child task", event_context=CTX)

    asyncio.run(seed())
    _resumed_parent, resumed = _resumed_runtime(parent, locator, child_logs)

    async def run() -> None:
        repaired = await resumed.repair_dangling_children()

        assert len(repaired) == 1
        assert repaired[0].status == "failed"
        failed = [
            event
            for event in parent.event_log.iter()
            if isinstance(event, SubagentRunFailedEvent)
        ]
        assert failed[-1].reason_code == "child_run_start_not_committed"

    asyncio.run(run())


def test_cancel_stops_running_child_task(tmp_path) -> None:
    started = asyncio.Event()

    async def child_runner(_runtime: SubagentRuntime, _run) -> None:
        started.set()
        await asyncio.Event().wait()

    _parent, _locator, _child_logs, runtime = _runtime(
        tmp_path, child_runner=child_runner
    )

    async def run() -> None:
        subagent = await runtime.spawn_agent(task="long child task", event_context=CTX)
        await asyncio.wait_for(started.wait(), timeout=1)
        handle = runtime._execution_registry.get(subagent.subagent_run_id)  # noqa: SLF001
        assert handle is not None and handle.coroutine is not None
        task = handle.coroutine

        await runtime.cancel(
            subagent.subagent_run_id, event_context=CTX, reason_code="test_cancel"
        )
        await asyncio.sleep(0)

        assert task.cancelled()

    asyncio.run(run())


def test_child_timeout_marks_subagent_failed(tmp_path) -> None:
    async def child_runner(_runtime: SubagentRuntime, _run) -> None:
        await asyncio.Event().wait()

    parent, _locator, _child_logs, runtime = _runtime(
        tmp_path,
        budget=SubagentBudget(child_timeout_seconds=0.001),
        child_runner=child_runner,
    )

    async def run() -> None:
        await runtime.spawn_agent(task="timed child task", event_context=CTX)
        for _ in range(20):
            failed = [
                event
                for event in parent.event_log.iter()
                if isinstance(event, SubagentRunFailedEvent)
            ]
            if failed:
                assert failed[-1].reason_code == "subagent_timeout"
                assert runtime.runs[0].status == "failed"
                return
            await asyncio.sleep(0.001)
        raise AssertionError("subagent timeout did not produce a failure event")

    asyncio.run(run())


def test_child_runner_failure_durable_diagnostic_redacts_exception_text(
    tmp_path,
) -> None:
    secret = "/private/secret/path API_KEY=do-not-persist"

    async def child_runner(_runtime: SubagentRuntime, _run) -> None:
        raise RuntimeError(secret)

    parent, _locator, _child_logs, runtime = _runtime(
        tmp_path, child_runner=child_runner
    )

    async def run() -> None:
        await runtime.spawn_agent(task="failing child task", event_context=CTX)
        for _ in range(20):
            failed = [
                event
                for event in parent.event_log.iter()
                if isinstance(event, SubagentRunFailedEvent)
            ]
            if failed:
                event = failed[-1]
                assert event.reason_code == "subagent_child_runner_error"
                payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
                assert secret not in payload
                assert event.diagnostics == [{"error_type": "RuntimeError"}]
                return
            await asyncio.sleep(0.001)
        raise AssertionError("child runner failure did not produce a failure event")

    asyncio.run(run())


def test_cancel_is_idempotent_for_completed_child(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)

    async def run() -> None:
        subagent = await runtime.spawn_fake(task="child task", event_context=CTX)
        await runtime.complete_fake(
            subagent.subagent_run_id, summary="done", event_context=CTX
        )

        cancelled = await runtime.cancel(
            subagent.subagent_run_id, event_context=CTX, reason_code="test_cancel"
        )

        assert cancelled.status == "completed"
        assert not any(
            isinstance(event, SubagentRunCancelledEvent)
            for event in parent.event_log.iter()
        )

    asyncio.run(run())


def test_safety_narrowing_cancels_active_children_sync(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)

    async def seed() -> None:
        await runtime.spawn_fake(task="child task", event_context=CTX)

    asyncio.run(seed())

    cancelled = runtime.fail_active_children_for_safety_narrowing_now(
        reason_code="subagent_bypass_revoked",
        reason_message="test narrowing",
    )

    assert len(cancelled) == 1
    assert cancelled[0].status == "cancelled"
    cancellation_event = [
        event
        for event in parent.event_log.iter()
        if isinstance(event, SubagentRunCancelledEvent)
    ][-1]
    assert cancellation_event.reason_code == "subagent_bypass_revoked"
    assert cancellation_event.cancelled_by == "runtime"


def test_safety_narrowing_sync_terminalizes_task_and_blocks_dependents(
    tmp_path,
) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    tool = CreateAgentTasksTool(runtime)

    async def seed() -> None:
        result = await tool.execute_async(
            ToolCall(
                id="tool:create-tasks",
                name="create_agent_tasks",
                arguments={
                    "tasks": [
                        {"task_key": "a", "profile": "review_worker", "task": "A"},
                        {
                            "task_key": "b",
                            "profile": "review_worker",
                            "task": "B",
                            "depends_on": ["a"],
                        },
                    ]
                },
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id, event_context=CTX
            ),
        )
        assert result.status is ToolResultState.SUCCESS

    asyncio.run(seed())

    cancelled = runtime.fail_active_children_for_safety_narrowing_now(
        reason_code="subagent_bypass_revoked",
        reason_message="test narrowing",
    )

    assert len(cancelled) == 1
    graph = runtime.graph()
    by_key = {task.task_key: task for task in graph.tasks}
    assert by_key["a"].status == "cancelled"
    assert by_key["b"].status == "blocked_dependency_failed"
    assert by_key["b"].blocked_by_task_ids == (by_key["a"].task_id,)
    assert by_key["b"].dependency_status_snapshot == {by_key["a"].task_id: "cancelled"}
    cancelled_event = next(
        event
        for event in parent.event_log.iter()
        if isinstance(event, SubagentTaskCancelledEvent)
        and event.task_id == by_key["a"].task_id
    )
    assert (
        by_key["b"].dependency_terminal_event_ids[by_key["a"].task_id]
        == cancelled_event.id
    )
    assert any(
        isinstance(event, SubagentTaskCancelledEvent)
        for event in parent.event_log.iter()
    )
    blocked_events = [
        event
        for event in parent.event_log.iter()
        if isinstance(event, SubagentTaskBlockedEvent)
    ]
    assert (
        blocked_events[-1].dependency_terminal_event_ids
        == by_key["b"].dependency_terminal_event_ids
    )


def test_cancel_sync_async_fact_equality(tmp_path) -> None:
    async_parent, _locator, _logs, async_runtime = _runtime(tmp_path / "async")
    sync_parent, _locator2, _logs2, sync_runtime = _runtime(tmp_path / "sync")
    async_tool = CreateAgentTasksTool(async_runtime)
    sync_tool = CreateAgentTasksTool(sync_runtime)

    async def seed(runtime, tool, parent) -> None:
        result = await tool.execute_async(
            ToolCall(
                id="tool:create-cancel-equality",
                name="create_agent_tasks",
                arguments={
                    "tasks": [
                        {"task_key": "a", "profile": "review_worker", "task": "A"},
                        {
                            "task_key": "b",
                            "profile": "review_worker",
                            "task": "B",
                            "depends_on": ["a"],
                        },
                    ]
                },
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id,
                event_context=CTX,
            ),
        )
        assert result.status is ToolResultState.SUCCESS

    asyncio.run(seed(async_runtime, async_tool, async_parent))
    asyncio.run(seed(sync_runtime, sync_tool, sync_parent))
    async_run_id = async_runtime.runs[0].subagent_run_id
    asyncio.run(
        async_runtime.fail_active_children_for_safety_narrowing(
            reason_code="subagent_safety_narrowed",
        )
    )
    sync_runtime.fail_active_children_for_safety_narrowing_now(
        reason_code="subagent_safety_narrowed",
    )

    def normalized(runtime) -> tuple[tuple[str | None, str, tuple[str, ...]], ...]:
        graph = runtime.graph()
        return tuple(
            sorted(
                (
                    task.task_key,
                    task.status,
                    tuple(
                        sorted(
                            next(
                                dependency.task_key or dependency.task_id
                                for dependency in graph.tasks
                                if dependency.task_id == dependency_id
                            )
                            for dependency_id in task.blocked_by_task_ids
                        )
                    ),
                )
                for task in graph.tasks
            )
        )

    assert async_run_id
    assert normalized(async_runtime) == normalized(sync_runtime)
    assert {run.status for run in async_runtime.runs} == {"cancelled"}
    assert {run.status for run in sync_runtime.runs} == {"cancelled"}


def test_transitive_cancel_block_events_use_real_event_ids(tmp_path) -> None:
    parent, _locator, _logs, runtime = _runtime(tmp_path)
    tool = CreateAgentTasksTool(runtime)

    async def run() -> None:
        result = await tool.execute_async(
            ToolCall(
                id="tool:create-real-refs",
                name="create_agent_tasks",
                arguments={
                    "tasks": [
                        {"task_key": "a", "profile": "review_worker", "task": "A"},
                        {
                            "task_key": "b",
                            "profile": "review_worker",
                            "task": "B",
                            "depends_on": ["a"],
                        },
                        {
                            "task_key": "c",
                            "profile": "review_worker",
                            "task": "C",
                            "depends_on": ["b"],
                        },
                    ]
                },
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent.runtime_session_id,
                event_context=CTX,
            ),
        )
        payload = json.loads(result.output)
        run_id = next(
            item["subagent_run_id"]
            for item in payload["tasks"]
            if item["task_key"] == "a"
        )
        await runtime.cancel(run_id, event_context=CTX, reason_code="test_cancel")

    asyncio.run(run())
    events_by_id = {event.id: event for event in parent.event_log.iter()}
    graph = runtime.graph()
    tasks = {task.task_key: task for task in graph.tasks}
    for downstream_key, upstream_key in (("b", "a"), ("c", "b")):
        downstream = tasks[downstream_key]
        upstream = tasks[upstream_key]
        terminal_event_id = downstream.dependency_terminal_event_ids[upstream.task_id]
        assert terminal_event_id in events_by_id
        assert events_by_id[terminal_event_id].id == terminal_event_id


def test_host_session_close_cancels_active_subagents(tmp_path) -> None:
    started = asyncio.Event()
    child_finalized = asyncio.Event()
    runtime_wiring = build_in_memory_runtime_wiring(tmp_path)
    _append_parent_run_start(
        runtime_wiring.runtime_session.event_log,
        runtime_session_id=runtime_wiring.runtime_session.runtime_session_id,
        event_context=CTX,
    )
    locator = InMemoryEventLogLocator()

    def child_event_log_factory(runtime_session_id: str) -> InMemoryEventLog:
        event_log = InMemoryEventLog()
        locator.register(runtime_session_id, event_log)
        return event_log

    async def child_runner(_runtime: SubagentRuntime, _run) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            child_finalized.set()

    subagent_runtime = SubagentRuntime(
        parent_runtime_session=runtime_wiring.runtime_session,
        child_event_log_factory=child_event_log_factory,
        event_log_locator=locator,
        child_runner=child_runner,
    )
    transport = _SubagentScriptedTransport()
    registry = LLMTransportRegistry()
    registry.register(transport)
    agent = AgentRuntime(
        runtime_session=runtime_wiring.runtime_session,
        llm_runtime=LLMRuntime(
            config=_subagent_test_llm_config(
                api_key="sk-test",
                base_url="https://example.test/v1",
                pro_model="pro",
                flash_model="flash",
                api="subagent-scripted",
            ),
            registry=registry,
        ),
        capability_runtime=CapabilityRuntime(),
        subagent_runtime=subagent_runtime,
        enable_subagents=False,
    )
    session = HostSession(
        host_session_id="host:test",
        conversation_id="conversation:test",
        workspace=resolve_workspace(
            HostWorkspaceInput(workspace_root=tmp_path, workspace_kind="project")
        ),
        wiring=AgentRuntimeWiring(agent_runtime=agent, runtime_wiring=runtime_wiring),
    )

    async def run() -> None:
        subagent = await subagent_runtime.spawn_agent(
            task="long child task", event_context=CTX
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        handle = subagent_runtime._execution_registry.get(subagent.subagent_run_id)  # noqa: SLF001
        assert handle is not None and handle.coroutine is not None
        task = handle.coroutine

        await session.aclose()

        assert task.cancelled()
        assert child_finalized.is_set()
        assert subagent_runtime._execution_registry.handles() == ()  # noqa: SLF001
        cancelled = [
            event
            for event in runtime_wiring.runtime_session.event_log.iter()
            if isinstance(event, SubagentRunCancelledEvent)
        ]
        assert cancelled[-1].reason_code == "subagent_host_session_close"
        assert cancelled[-1].cancelled_by == "host_shutdown"

    asyncio.run(run())


def test_host_permission_leaving_bypass_does_not_cancel_active_subagents(
    tmp_path,
) -> None:
    started = asyncio.Event()
    runtime_wiring = build_in_memory_runtime_wiring(tmp_path)
    _append_parent_run_start(
        runtime_wiring.runtime_session.event_log,
        runtime_session_id=runtime_wiring.runtime_session.runtime_session_id,
        event_context=CTX,
    )
    locator = InMemoryEventLogLocator()

    def child_event_log_factory(runtime_session_id: str) -> InMemoryEventLog:
        event_log = InMemoryEventLog()
        locator.register(runtime_session_id, event_log)
        return event_log

    async def child_runner(_runtime: SubagentRuntime, _run) -> None:
        started.set()
        await asyncio.Event().wait()

    subagent_runtime = SubagentRuntime(
        parent_runtime_session=runtime_wiring.runtime_session,
        child_event_log_factory=child_event_log_factory,
        event_log_locator=locator,
        child_runner=child_runner,
    )
    transport = _SubagentScriptedTransport()
    registry = LLMTransportRegistry()
    registry.register(transport)
    agent = AgentRuntime(
        runtime_session=runtime_wiring.runtime_session,
        llm_runtime=LLMRuntime(
            config=_subagent_test_llm_config(
                api_key="sk-test",
                base_url="https://example.test/v1",
                pro_model="pro",
                flash_model="flash",
                api="subagent-scripted",
            ),
            registry=registry,
        ),
        capability_runtime=CapabilityRuntime(),
        subagent_runtime=subagent_runtime,
        enable_subagents=False,
    )
    session = HostSession(
        host_session_id="host:test",
        conversation_id="conversation:test",
        workspace=resolve_workspace(
            HostWorkspaceInput(workspace_root=tmp_path, workspace_kind="project")
        ),
        wiring=AgentRuntimeWiring(agent_runtime=agent, runtime_wiring=runtime_wiring),
    )

    async def run() -> None:
        subagent = await subagent_runtime.spawn_agent(
            task="long child task", event_context=CTX
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        handle = subagent_runtime._execution_registry.get(subagent.subagent_run_id)  # noqa: SLF001
        assert handle is not None and handle.coroutine is not None
        task = handle.coroutine

        session.set_permission_mode("read-only")
        await asyncio.sleep(0)

        assert not task.cancelled()
        cancelled = [
            event
            for event in runtime_wiring.runtime_session.event_log.iter()
            if isinstance(event, SubagentRunCancelledEvent)
        ]
        assert not cancelled
        await subagent_runtime.cancel(
            subagent.subagent_run_id,
            event_context=CTX,
            reason_code="test_cleanup",
            cancelled_by="runtime",
        )

    asyncio.run(run())


def test_subagent_events_round_trip_through_agent_event_serialization(tmp_path) -> None:
    _parent, _locator, _child_logs, runtime = _runtime(tmp_path)

    async def run() -> None:
        subagent = await runtime.spawn_fake(task="child task", event_context=CTX)
        await runtime.complete_fake(
            subagent.subagent_run_id, summary="done", event_context=CTX
        )

    asyncio.run(run())

    for event in runtime.parent_runtime_session.event_log.iter():
        assert load_agent_event(dump_agent_event(event)) == event


def test_parent_transcript_rebuild_ignores_subagent_graph_events_after_run_end(
    tmp_path,
) -> None:
    parent, _locator, _child_logs, runtime = _runtime(
        tmp_path, seed_parent_run=False
    )

    async def run() -> None:
        from pulsara_agent.event import RunEndEvent, RunStartEvent

        await parent.emit(
            RunStartEvent(
                **CTX.event_fields(),
                **run_start_permission_fields(
                    CTX.run_id,
                    user_input="hello",
                    turn_id=CTX.turn_id,
                    reply_id=CTX.reply_id,
                    mcp_installation_owner_runtime_session_id="runtime:parent",
                ),
                user_input_chars=5,
                metadata={"user_input": "hello"},
            )
        )
        await parent.emit(
            RunEndEvent(
                **run_end_contract_fields(CTX.run_id, status="finished"),
                **CTX.event_fields(),
                status="finished",
                stop_reason="final",
            )
        )
        subagent = await runtime.spawn_fake(task="child task", event_context=CTX)
        await runtime.complete_fake(subagent.subagent_run_id, summary="child done")

    asyncio.run(run())

    messages = rebuild_prior_messages(
        parent.event_log, archive=parent.archive, session_id=parent.runtime_session_id
    )
    assert [message.id for message in messages] == [f"user-message:{CTX.run_id}"]


class _SubagentScriptedTransport:
    api = "subagent-scripted"
    binding_id = "test.subagent-scripted"
    contract_version = "v1"

    def __init__(self) -> None:
        self.contexts: list[LLMContext] = []

    async def stream(
        self,
        *,
        call,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[AgentEvent]:
        del call
        self.contexts.append(context)
        text = _context_text(context)
        if "CHILD TASK: summarize the moon" in text:
            async for event in _text_reply(
                event_context, "child says: the moon is bright"
            ):
                yield event
        elif '"result_id"' in text:
            async for event in _text_reply(
                event_context, "parent received child result"
            ):
                yield event
        elif '"status": "started"' in text:
            subagent_run_id = _extract_subagent_run_id(text)
            async for event in _tool_reply(
                event_context,
                tool_call_id="tool:wait-child",
                name="wait_agent",
                arguments={"subagent_run_id": subagent_run_id, "timeout_seconds": 1},
            ):
                yield event
        else:
            async for event in _tool_reply(
                event_context,
                tool_call_id="tool:spawn-child",
                name="spawn_agent",
                arguments={
                    "task": "CHILD TASK: summarize the moon",
                    "label": "moon-worker",
                    "role": "worker",
                    "context": "isolated",
                },
            ):
                yield event


class _FinalOnlyTransport:
    api = "final-only"
    binding_id = "test.final-only"
    contract_version = "v1"

    async def stream(
        self,
        *,
        call,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[AgentEvent]:
        del call, context
        async for event in _text_reply(event_context, "parent final"):
            yield event


class _PendingChildTransport:
    api = "pending-child"
    binding_id = "test.pending-child"
    contract_version = "v1"

    async def stream(
        self,
        *,
        call,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[AgentEvent]:
        del call
        text = _context_text(context)
        if "CHILD NEEDS PLAN QUESTION" in text and '"status": "entered"' not in text:
            async for event in _tool_reply(
                event_context,
                tool_call_id="tool:child-enter-plan",
                name="enter_plan",
                arguments={"reason": "child wants to ask the user"},
            ):
                yield event
        elif "CHILD NEEDS PLAN QUESTION" in text and '"status": "entered"' in text:
            async for event in _tool_reply(
                event_context,
                tool_call_id="tool:child-plan-question",
                name="ask_plan_question",
                arguments={
                    "question": "Should the child continue?",
                    "allow_free_text": True,
                },
            ):
                yield event
        elif '"status": "started"' in text:
            async for event in _text_reply(
                event_context, "parent finished after spawn"
            ):
                yield event
        else:
            async for event in _tool_reply(
                event_context,
                tool_call_id="tool:spawn-pending-child",
                name="spawn_agent",
                arguments={"task": "CHILD NEEDS PLAN QUESTION"},
            ):
                yield event


class _BatchRepairTransport:
    api = "batch-repair"
    binding_id = "test.batch-repair"
    contract_version = "v1"

    def __init__(self) -> None:
        self.allow_parent_final = asyncio.Event()

    async def stream(
        self,
        *,
        call,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[AgentEvent]:
        del call
        text = _context_text(context)
        if "BATCH CHILD A" in text:
            async for event in _text_reply(event_context, "child A native result"):
                yield event
        elif '"status": "accepted"' in text:
            await self.allow_parent_final.wait()
            async for event in _text_reply(event_context, "parent after batch repair"):
                yield event
        else:
            async for event in _tool_reply(
                event_context,
                tool_call_id="tool:create-batch-repair",
                name="create_agent_tasks",
                arguments={
                    "tasks": [
                        {
                            "task_key": "a",
                            "profile": "review_worker",
                            "task": "BATCH CHILD A",
                        },
                        {
                            "task_key": "b",
                            "profile": "review_worker",
                            "task": "BATCH CHILD B",
                        },
                    ]
                },
            ):
                yield event


class _ListAgentsNonBypassTransport:
    api = "list-agents-non-bypass"
    binding_id = "test.list-agents-non-bypass"
    contract_version = "v1"

    def __init__(self) -> None:
        self.contexts: list[LLMContext] = []

    async def stream(
        self,
        *,
        call,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[AgentEvent]:
        del call
        self.contexts.append(context)
        if "subagent_requires_bypass_mode" in _context_text(context):
            async for event in _text_reply(
                event_context, "list_agents denied outside bypass"
            ):
                yield event
        else:
            async for event in _tool_reply(
                event_context,
                tool_call_id="tool:list-agents",
                name="list_agents",
                arguments={},
            ):
                yield event


def test_list_agents_is_visible_but_gate_denied_outside_bypass(tmp_path) -> None:
    transport = _ListAgentsNonBypassTransport()
    registry = LLMTransportRegistry()
    registry.register(transport)
    parent_session = in_memory_runtime_session(
        tmp_path, runtime_session_id="runtime:parent"
    )
    agent = AgentRuntime(
        runtime_session=parent_session,
        llm_runtime=LLMRuntime(
            config=_subagent_test_llm_config(
                api_key="sk-test",
                base_url="https://example.test/v1",
                pro_model="pro",
                flash_model="flash",
                api="list-agents-non-bypass",
            ),
            registry=registry,
        ),
        capability_runtime=CapabilityRuntime(),
        permission_policy=preset_to_policy(PermissionMode.READ_ONLY),
    )

    result = asyncio.run(run_agent_task(agent, "list children"))

    assert result.status is LoopStatus.FINISHED
    assert result.final_text == "list_agents denied outside bypass"
    gate_decision = next(
        event
        for event in parent_session.event_log.iter()
        if isinstance(event, CapabilityGateDecisionEvent)
        and event.tool_call_id == "tool:list-agents"
    )
    assert gate_decision.tool_name == "list_agents"
    assert gate_decision.decision == "deny"
    assert gate_decision.result_state is ToolResultState.DENIED
    assert gate_decision.reason_code == "subagent_requires_bypass_mode"
    assert gate_decision.permission_category == "subagent_runtime"
    assert any(
        "list_agents" in getattr(tool, "name", "")
        for tool in agent.tool_executor.registry.all()
    )


def test_agent_runtime_repairs_dangling_subagent_before_turn(tmp_path) -> None:
    parent, locator, child_logs, runtime = _runtime(tmp_path)

    async def seed() -> None:
        await runtime.spawn_fake(task="lost child task", event_context=CTX)
        through_sequence = max(
            (event.sequence or 0 for event in parent.event_log.iter()), default=0
        )
        await parent.subagent_graph_checkpoint_service.restore_for_selection(
            requested_through_sequence=through_sequence
        )

    asyncio.run(seed())
    resumed_parent, resumed = _resumed_runtime(parent, locator, child_logs)
    transport = _FinalOnlyTransport()
    registry = LLMTransportRegistry()
    registry.register(transport)
    agent = AgentRuntime(
        runtime_session=resumed_parent,
        llm_runtime=LLMRuntime(
            config=_subagent_test_llm_config(
                api_key="sk-test",
                base_url="https://example.test/v1",
                pro_model="pro",
                flash_model="flash",
                api="final-only",
            ),
            registry=registry,
        ),
        capability_runtime=CapabilityRuntime(),
        subagent_runtime=resumed,
    )

    result = asyncio.run(run_agent_task(agent, "resume after lost child"))

    assert result.status is LoopStatus.FINISHED
    failed = [
        event
        for event in parent.event_log.iter()
        if isinstance(event, SubagentRunFailedEvent)
    ]
    assert failed[-1].reason_code == "child_run_start_not_committed"


def test_child_enter_plan_finalizes_without_parent_pending_slot(tmp_path) -> None:
    transport = _PendingChildTransport()
    registry = LLMTransportRegistry()
    registry.register(transport)
    parent_session = in_memory_runtime_session(
        tmp_path, runtime_session_id="runtime:parent"
    )
    agent = AgentRuntime(
        runtime_session=parent_session,
        llm_runtime=LLMRuntime(
            config=_subagent_test_llm_config(
                api_key="sk-test",
                base_url="https://example.test/v1",
                pro_model="pro",
                flash_model="flash",
                api="pending-child",
            ),
            registry=registry,
        ),
        capability_runtime=CapabilityRuntime(),
    )

    async def run_parent() -> None:
        result = await run_agent_task(agent, "spawn a child that needs approval")
        assert result.status is LoopStatus.FINISHED
        assert result.state.pending_interaction_kind is None
        for _ in range(200):
            if any(
                isinstance(event, SubagentRunCompletedEvent)
                and event.summary == "(child agent finished without final text)"
                for event in parent_session.event_log.iter()
            ):
                return
            await asyncio.sleep(0.001)
        raise AssertionError("child enter_plan completion was not recorded")

    asyncio.run(run_parent())


def test_child_run_start_none_atomically_settles_parent_reservation_zero(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_before_child_run_start(*_args, **_kwargs):
        raise RuntimeError("synthetic child RunStart failure")

    monkeypatch.setattr(
        SubagentRunEntryDriver,
        "prepare_and_commit",
        fail_before_child_run_start,
    )
    transport = _PendingChildTransport()
    registry = LLMTransportRegistry()
    registry.register(transport)
    parent_session = in_memory_runtime_session(
        tmp_path, runtime_session_id="runtime:parent"
    )
    agent = AgentRuntime(
        runtime_session=parent_session,
        llm_runtime=LLMRuntime(
            config=_subagent_test_llm_config(
                api_key="sk-test",
                base_url="https://example.test/v1",
                pro_model="pro",
                flash_model="flash",
                api="pending-child",
            ),
            registry=registry,
        ),
        capability_runtime=CapabilityRuntime(),
    )

    async def run_parent() -> None:
        result = await run_agent_task(agent, "spawn a child that cannot start")
        assert result.status is LoopStatus.FINISHED
        for _ in range(200):
            if any(
                isinstance(event, SubagentRunFailedEvent)
                and event.reason_code == "subagent_child_runner_error"
                for event in parent_session.event_log.iter()
            ):
                break
            await asyncio.sleep(0.001)
        else:
            raise AssertionError("child start failure was not recorded")

    asyncio.run(run_parent())

    parent_events = parent_session.event_log.iter()
    failed = next(
        event
        for event in parent_events
        if isinstance(event, SubagentRunFailedEvent)
        and event.reason_code == "subagent_child_runner_error"
    )
    settlement = next(
        event
        for event in parent_events
        if isinstance(event, RolloutBudgetReservationSettledEvent)
        and event.usage_status == "child_not_started_zero"
    )
    assert settlement.sequence == failed.sequence + 1
    assert settlement.charged_milliunits == 0
    assert settlement.child_usage_handoff is None
    parent_start = next(
        event
        for event in parent_events
        if isinstance(event, RunStartEvent) and event.run_id == failed.run_id
    )
    account_state = parent_session.long_horizon_state_store.rollout_state(
        parent_start.long_horizon.rollout_account_id
    )
    assert account_state is not None
    assert not any(
        reservation.owner_id == failed.subagent_run_id
        for reservation in account_state.active_reservations
    )
    assert agent.subagent_runtime is not None
    assert not any(
        isinstance(event, (RunStartEvent, RunEndEvent))
        for event in agent.subagent_runtime.child_event_log(
            failed.subagent_run_id
        ).iter()
    )


def test_native_child_cancel_keeps_owner_until_atomic_parent_handoff(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_started = asyncio.Event()
    original_run_committed_entry = AgentRuntime.run_committed_entry

    async def block_child_run(
        runtime,
        draft,
        committed,
        *,
        active_skill_names=None,
    ):
        if not runtime._is_subagent_child:  # noqa: SLF001
            return await original_run_committed_entry(
                runtime,
                draft,
                committed,
                active_skill_names=active_skill_names,
            )
        child_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await runtime.fail_committed_run(
                draft.state,
                stop_reason=RunStopReason.RUNTIME_EXECUTION_ERROR,
                error_message="child cancelled by parent",
            )
            raise

    monkeypatch.setattr(AgentRuntime, "run_committed_entry", block_child_run)
    transport = _PendingChildTransport()
    registry = LLMTransportRegistry()
    registry.register(transport)
    parent_session = in_memory_runtime_session(
        tmp_path, runtime_session_id="runtime:parent"
    )
    agent = AgentRuntime(
        runtime_session=parent_session,
        llm_runtime=LLMRuntime(
            config=_subagent_test_llm_config(
                api_key="sk-test",
                base_url="https://example.test/v1",
                pro_model="pro",
                flash_model="flash",
                api="pending-child",
            ),
            registry=registry,
        ),
        capability_runtime=CapabilityRuntime(),
    )

    async def run_parent_and_cancel() -> None:
        parent_task = asyncio.create_task(
            run_agent_task(agent, "spawn a child and then finish")
        )
        await asyncio.wait_for(child_started.wait(), timeout=1)
        assert agent.subagent_runtime is not None
        run = next(
            item for item in agent.subagent_runtime.runs if item.status == "running"
        )
        handle = agent.subagent_runtime._execution_registry.get(  # noqa: SLF001
            run.subagent_run_id
        )
        assert handle is not None and handle.coroutine is not None

        original_write_events = RuntimeSession.write_events
        failed_once = False

        async def fail_first_parent_terminal(runtime_session, events, **kwargs):
            nonlocal failed_once
            if runtime_session is parent_session and not failed_once and any(
                isinstance(event, SubagentRunCancelledEvent) for event in events
            ):
                failed_once = True
                raise RuntimeError("synthetic parent terminal commit NONE")
            return await original_write_events(runtime_session, events, **kwargs)

        monkeypatch.setattr(
            RuntimeSession,
            "write_events",
            fail_first_parent_terminal,
        )

        with pytest.raises(RuntimeError, match="parent terminal commit NONE"):
            await agent.subagent_runtime.cancel(
                run.subagent_run_id,
                reason_code="test_native_cancel",
                cancelled_by="parent_agent",
            )
        assert handle.coroutine.done()
        assert (
            agent.subagent_runtime._execution_registry.get(  # noqa: SLF001
                run.subagent_run_id
            )
            is handle
        )
        assert next(
            item
            for item in agent.subagent_runtime.runs
            if item.subagent_run_id == run.subagent_run_id
        ).status == "running"
        parent_start = next(
            event
            for event in parent_session.event_log.iter(run_id=run.parent_run_id)
            if isinstance(event, RunStartEvent)
        )
        account_state = parent_session.long_horizon_state_store.rollout_state(
            parent_start.long_horizon.rollout_account_id
        )
        assert account_state is not None
        assert any(
            reservation.owner_kind == "subagent_run"
            and reservation.owner_id == run.subagent_run_id
            for reservation in account_state.active_reservations
        )

        await agent.subagent_runtime.cancel(
            run.subagent_run_id,
            reason_code="test_native_cancel",
            cancelled_by="parent_agent",
        )

        assert handle.coroutine.cancelled()
        assert (
            agent.subagent_runtime._execution_registry.get(  # noqa: SLF001
                run.subagent_run_id
            )
            is None
        )
        result = await asyncio.wait_for(parent_task, timeout=2)
        assert result.status is LoopStatus.FINISHED

    asyncio.run(run_parent_and_cancel())

    parent_events = parent_session.event_log.iter()
    cancelled = next(
        event
        for event in parent_events
        if isinstance(event, SubagentRunCancelledEvent)
        and event.reason_code == "test_native_cancel"
    )
    settlement = next(
        event
        for event in parent_events
        if isinstance(event, RolloutBudgetReservationSettledEvent)
        and event.usage_status == "child_terminal_handoff"
    )
    assert settlement.sequence == cancelled.sequence + 1
    assert settlement.child_usage_handoff is not None
    terminal_ref = settlement.child_usage_handoff.child_terminal_reference
    assert terminal_ref == cancelled.child_terminal_reference
    assert settlement.child_usage_handoff.settlement_aggregate.charged_milliunits == 0
    assert agent.subagent_runtime is not None
    child_events = agent.subagent_runtime.child_event_log(
        cancelled.subagent_run_id
    ).iter()
    child_close = next(
        event
        for event in child_events
        if isinstance(event, ChildRolloutSubaccountClosedEvent)
    )
    child_end = next(event for event in child_events if isinstance(event, RunEndEvent))
    assert child_close.run_end_event_id == child_end.id == terminal_ref.terminal_event_id
    assert (
        child_close.settlement_aggregate
        == settlement.child_usage_handoff.settlement_aggregate
    )


def test_materialized_batch_repair_atomically_settles_mixed_children_after_cancel(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _BatchRepairTransport()
    registry = LLMTransportRegistry()
    registry.register(transport)
    parent_session = in_memory_runtime_session(
        tmp_path, runtime_session_id="runtime:parent"
    )
    agent = AgentRuntime(
        runtime_session=parent_session,
        llm_runtime=LLMRuntime(
            config=_subagent_test_llm_config(
                api_key="sk-test",
                base_url="https://example.test/v1",
                pro_model="pro",
                flash_model="flash",
                api="batch-repair",
            ),
            registry=registry,
        ),
        capability_runtime=CapabilityRuntime(),
    )
    assert agent.subagent_runtime is not None
    subagent_runtime = agent.subagent_runtime
    original_child_runner = agent._run_child_agent  # noqa: SLF001
    original_complete = subagent_runtime.complete_native_result
    child_a_run_ids: set[str] = set()
    child_a_native_terminal = asyncio.Event()
    child_b_pre_start = asyncio.Event()

    async def controlled_child_runner(runtime, run_view):
        if run_view.task_text == "BATCH CHILD B":
            child_b_pre_start.set()
            await asyncio.Event().wait()
        child_a_run_ids.add(run_view.fact.subagent_run_id)
        await original_child_runner(runtime, run_view)

    async def hold_child_a_after_native_terminal(
        subagent_run_id: str,
        *,
        child_run_id: str,
    ):
        if subagent_run_id in child_a_run_ids:
            child_a_native_terminal.set()
            await asyncio.Event().wait()
        return await original_complete(
            subagent_run_id,
            child_run_id=child_run_id,
        )

    subagent_runtime.bind_child_runner(controlled_child_runner)
    monkeypatch.setattr(
        subagent_runtime,
        "complete_native_result",
        hold_child_a_after_native_terminal,
    )
    committed_repair_batches: list[tuple[AgentEvent, ...]] = []
    original_write_events = RuntimeSession.write_events

    async def capture_repair_batch(runtime_session, events, **kwargs):
        outcome = await original_write_events(runtime_session, events, **kwargs)
        if runtime_session is parent_session and sum(
            isinstance(event, SubagentRunCancelledEvent) for event in events
        ) == 2:
            committed_repair_batches.append(tuple(events))
            raise asyncio.CancelledError
        return outcome

    monkeypatch.setattr(RuntimeSession, "write_events", capture_repair_batch)

    async def run_parent_and_repair() -> None:
        parent_task = asyncio.create_task(
            run_agent_task(agent, "create a two-child batch")
        )
        try:
            await asyncio.wait_for(child_a_native_terminal.wait(), timeout=10)
            await asyncio.wait_for(child_b_pre_start.wait(), timeout=10)
            runs = tuple(subagent_runtime.runs)
            assert len(runs) == 2
            assert len({run.batch_id for run in runs}) == 1
            batch_id = runs[0].batch_id
            assert batch_id is not None
            assert runs[0].parent_turn_id is not None
            assert runs[0].parent_reply_id is not None

            with pytest.raises(asyncio.CancelledError):
                await subagent_runtime.repair_materialized_batch(
                    batch_id,
                    event_context=EventContext(
                        run_id=runs[0].parent_run_id,
                        turn_id=runs[0].parent_turn_id,
                        reply_id=runs[0].parent_reply_id,
                    ),
                    repair_id="repair:mixed-child-start-state",
                    reason_code="subagent_task_batch_start_failed",
                    reason_message="synthetic mixed child repair",
                )
        finally:
            transport.allow_parent_final.set()
        result = await asyncio.wait_for(parent_task, timeout=10)
        assert result.status is LoopStatus.FINISHED
        assert result.final_text == "parent after batch repair"

    asyncio.run(run_parent_and_repair())

    assert len(committed_repair_batches) == 1
    repair_batch = committed_repair_batches[0]
    run_terminals = tuple(
        event for event in repair_batch if isinstance(event, SubagentRunCancelledEvent)
    )
    task_terminals = tuple(
        event for event in repair_batch if isinstance(event, SubagentTaskCancelledEvent)
    )
    settlements = tuple(
        event
        for event in repair_batch
        if isinstance(event, RolloutBudgetReservationSettledEvent)
    )
    assert len(run_terminals) == len(task_terminals) == len(settlements) == 2
    assert {event.usage_status for event in settlements} == {
        "child_terminal_handoff",
        "child_not_started_zero",
    }
    handoff_settlement = next(
        event
        for event in settlements
        if event.usage_status == "child_terminal_handoff"
    )
    zero_settlement = next(
        event
        for event in settlements
        if event.usage_status == "child_not_started_zero"
    )
    assert handoff_settlement.child_usage_handoff is not None
    assert zero_settlement.child_usage_handoff is None
    assert zero_settlement.charged_milliunits == 0
    assert any(
        event.child_terminal_reference
        == handoff_settlement.child_usage_handoff.child_terminal_reference
        for event in run_terminals
    )

    tasks_by_key = {task.task_key: task for task in subagent_runtime.tasks}
    child_a_run_id = tasks_by_key["a"].current_run_id
    child_b_run_id = tasks_by_key["b"].current_run_id
    assert child_a_run_id is not None and child_b_run_id is not None
    child_a_events = subagent_runtime.child_event_log(child_a_run_id).iter()
    child_b_events = subagent_runtime.child_event_log(child_b_run_id).iter()
    assert any(isinstance(event, RunStartEvent) for event in child_a_events)
    assert any(isinstance(event, RunEndEvent) for event in child_a_events)
    assert any(
        isinstance(event, ChildRolloutSubaccountClosedEvent)
        for event in child_a_events
    )
    assert not any(isinstance(event, RunStartEvent) for event in child_b_events)
    assert not any(isinstance(event, RunEndEvent) for event in child_b_events)
    assert {task.status for task in subagent_runtime.tasks} == {"cancelled"}
    assert {run.status for run in subagent_runtime.runs} == {"cancelled"}
    assert (
        subagent_runtime._execution_registry.get(child_a_run_id) is None  # noqa: SLF001
    )
    assert (
        subagent_runtime._execution_registry.get(child_b_run_id) is None  # noqa: SLF001
    )

    parent_start = next(
        event
        for event in parent_session.event_log.iter()
        if isinstance(event, RunStartEvent)
    )
    account_state = parent_session.long_horizon_state_store.rollout_state(
        parent_start.long_horizon.rollout_account_id
    )
    assert account_state is not None
    assert not {
        child_a_run_id,
        child_b_run_id,
    } & {reservation.owner_id for reservation in account_state.active_reservations}


def test_agent_runtime_can_spawn_real_child_runtime_and_wait_result(tmp_path) -> None:
    transport = _SubagentScriptedTransport()
    registry = LLMTransportRegistry()
    registry.register(transport)
    llm_runtime = LLMRuntime(
        config=_subagent_test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="subagent-scripted",
        ),
        registry=registry,
    )
    parent_session = in_memory_runtime_session(
        tmp_path, runtime_session_id="runtime:parent"
    )
    agent = AgentRuntime(
        runtime_session=parent_session,
        llm_runtime=llm_runtime,
        capability_runtime=CapabilityRuntime(),
    )

    result = asyncio.run(run_agent_task(agent, "Parent: delegate the moon summary."))

    assert result.status is LoopStatus.FINISHED
    assert result.final_text == "parent received child result"
    assert agent.subagent_runtime is not None
    graph = agent.subagent_runtime.graph()
    assert len(graph.nodes) == 1
    assert graph.nodes[0].status == "completed"
    assert graph.nodes[0].consumed_by_wait is True
    assert graph.nodes[0].delivered is False
    assert graph.edges[0].source_tool_call_id == "tool:spawn-child"

    child_events = agent.subagent_runtime.child_event_log(
        graph.nodes[0].subagent_run_id
    ).iter()
    assert child_events
    assert all(event.metadata.get("subagent") for event in child_events)
    child_run_start = next(
        event for event in child_events if isinstance(event, RunStartEvent)
    )
    child_model_start = next(
        event for event in child_events if isinstance(event, ModelCallStartEvent)
    )
    assert child_run_start.model_target == child_model_start.resolved_call.target
    child_rollout_close = next(
        event
        for event in child_events
        if isinstance(event, ChildRolloutSubaccountClosedEvent)
    )
    parent_events = parent_session.event_log.iter()
    assert any(isinstance(event, SubagentRunStartedEvent) for event in parent_events)
    assert any(isinstance(event, SubagentRunCompletedEvent) for event in parent_events)
    assert any(isinstance(event, SubagentEdgeRecordedEvent) for event in parent_events)
    assert not any(
        isinstance(event, SubagentResultDeliveredEvent) for event in parent_events
    )
    started_event = next(
        event for event in parent_events if isinstance(event, SubagentRunStartedEvent)
    )
    wait_edge = next(
        event
        for event in parent_events
        if isinstance(event, SubagentEdgeRecordedEvent) and event.edge_kind == "wait"
    )
    assert started_event.parent_context_id == transport.contexts[0].context_id
    completed_event = next(
        event for event in parent_events if isinstance(event, SubagentRunCompletedEvent)
    )
    child_settlement = next(
        event
        for event in parent_events
        if isinstance(event, RolloutBudgetReservationSettledEvent)
        and event.usage_status == "child_terminal_handoff"
    )
    assert child_settlement.sequence == completed_event.sequence + 1
    assert child_settlement.child_usage_handoff is not None
    assert (
        child_settlement.child_usage_handoff.settlement_aggregate
        == child_rollout_close.settlement_aggregate
    )
    assert (
        child_settlement.child_usage_handoff.subaccount_fingerprint
        == child_rollout_close.subaccount_fingerprint
    )
    assert (
        child_settlement.charged_milliunits
        == child_rollout_close.settlement_aggregate.charged_milliunits
    )
    assert load_agent_event(dump_agent_event(child_settlement)) == child_settlement
    assert (
        started_event.parent_model_call_index == transport.contexts[0].model_call_index
    )
    wait_context = next(
        context
        for context in transport.contexts
        if '"status": "started"' in _context_text(context)
    )
    assert wait_edge.source_context_id == wait_context.context_id
    assert wait_edge.source_model_call_index == wait_context.model_call_index
    allowed_tool_names = set(started_event.capability_profile.allowed_tool_names)
    assert "spawn_agent" not in allowed_tool_names
    assert not any(
        name.startswith("memory_") or name.startswith("remember_")
        for name in allowed_tool_names
    )
    child_exposures = [
        event.exposure
        for event in child_events
        if isinstance(event, CapabilityExposureResolvedEvent)
    ]
    assert child_exposures
    callable_names = {
        entry.capability_name
        for entry in child_exposures[0].authorization_entries
        if entry.callable
    }
    assert "spawn_agent" not in callable_names
    assert "report_agent_result" in callable_names
    child_compiled = next(
        event for event in child_events if isinstance(event, ContextCompiledEvent)
    )
    assert child_compiled.input_audit is not None
    manifest = load_context_input_manifest(
        audit=child_compiled.input_audit,
        archive=parent_session.archive,
    )
    parent_ranges = manifest.snapshot.named_event_ranges
    assert parent_ranges
    assert all(
        item.runtime_session_id == parent_session.runtime_session_id
        for item in parent_ranges
    )
    assert tuple(item.first_sequence for item in parent_ranges) == tuple(
        sorted(item.first_sequence for item in parent_ranges)
    )
    covered_parent_sequences = {
        sequence
        for item in parent_ranges
        for sequence in range(item.first_sequence, item.through_sequence + 1)
    }
    assert 1 in covered_parent_sequences
    assert started_event.sequence in covered_parent_sequences
    child_log = agent.subagent_runtime.child_event_log(graph.nodes[0].subagent_run_id)
    child_read = child_log.read_raw_range_snapshot(
        minimum_sequence=child_compiled.input_audit.authority_from_sequence,
        through_sequence=child_compiled.input_audit.source_through_sequence,
    )
    child_slice = ContextEventSlice.from_read_snapshot(
        runtime_session_id=child_compiled.input_audit.source_runtime_session_id,
        minimum_sequence=child_compiled.input_audit.authority_from_sequence,
        snapshot=child_read,
    )
    parent_slices = tuple(
        ContextEventSlice.from_read_snapshot(
            runtime_session_id=parent_range.runtime_session_id,
            minimum_sequence=parent_range.first_sequence,
            snapshot=parent_session.event_log.read_raw_range_snapshot(
                minimum_sequence=parent_range.first_sequence,
                through_sequence=parent_range.through_sequence,
            ),
        )
        for parent_range in parent_ranges
    )
    assert (
        replay_compiled_context(
            event=child_compiled,
            archive=parent_session.archive,
            event_log=child_log,
            event_slice=child_slice,
            named_slices=parent_slices,
        ).status.value
        == "exact_replay"
    )


def test_inferred_child_result_repair_reproduces_non_default_policy_payload(
    tmp_path,
) -> None:
    transport = _SubagentScriptedTransport()
    registry = LLMTransportRegistry()
    registry.register(transport)
    parent = in_memory_runtime_session(
        tmp_path, runtime_session_id="runtime:parent:deterministic-child"
    )
    agent = AgentRuntime(
        runtime_session=parent,
        llm_runtime=LLMRuntime(
            config=_subagent_test_llm_config(
                api_key="sk-test",
                base_url="https://example.test/v1",
                pro_model="pro",
                flash_model="flash",
                api="subagent-scripted",
            ),
            registry=registry,
        ),
        capability_runtime=CapabilityRuntime(),
    )
    assert agent.subagent_runtime is not None
    budget = SubagentBudget(
        max_result_summary_chars_per_child=7,
        max_result_artifact_refs_per_child=1,
    )

    agent.subagent_runtime.default_budget = budget
    result = asyncio.run(run_agent_task(agent, "Parent: delegate the moon summary."))
    assert result.status is LoopStatus.FINISHED
    subagent_run_id = agent.subagent_runtime.runs[0].subagent_run_id
    original = next(
        event
        for event in parent.event_log.iter()
        if isinstance(event, SubagentRunCompletedEvent)
        and event.subagent_run_id == subagent_run_id
    )
    assert original.summary == "child …"
    assert len(original.artifact_ids) == 1

    truncated_log = InMemoryEventLog(runtime_session_id=parent.runtime_session_id)
    assert original.sequence is not None
    truncated_log.extend(
        event.model_copy(update={"sequence": None})
        for event in parent.event_log.iter()
        if event.sequence is not None and event.sequence < original.sequence
    )
    resumed_parent = RuntimeSession(
        parent.workspace_root,
        event_log=truncated_log,
        archive=parent.archive,
        tool_result_artifacts=parent.tool_result_artifacts,
        runtime_session_id=parent.runtime_session_id,
        terminal_binding=parent.terminal_binding,
        extra_tool_bindings=parent.extra_tool_bindings,
    )
    resumed = SubagentRuntime(
        parent_runtime_session=resumed_parent,
        child_event_log_factory=lambda runtime_session_id: (
            agent.subagent_runtime.event_log_locator.event_log_for_runtime_session(
                runtime_session_id
            )
        ),
        event_log_locator=agent.subagent_runtime.event_log_locator,
    )

    repaired = asyncio.run(resumed.repair_dangling_children())
    assert repaired[0].status == "completed"
    replayed = next(
        event
        for event in truncated_log.iter()
        if isinstance(event, SubagentRunCompletedEvent)
    )
    assert replayed.model_dump(
        exclude={"sequence"}, mode="json"
    ) == original.model_dump(exclude={"sequence"}, mode="json")


def test_subagent_child_run_records_model_target(tmp_path) -> None:
    test_agent_runtime_can_spawn_real_child_runtime_and_wait_result(tmp_path)


class _ExplicitReportChildTransport:
    api = "explicit-report-child"
    binding_id = "test.explicit-report-child"
    contract_version = "v1"

    def __init__(self) -> None:
        self.contexts: list[LLMContext] = []
        self.child_context_count = 0

    async def stream(
        self,
        *,
        call,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[AgentEvent]:
        del call
        self.contexts.append(context)
        text = _context_text(context)
        tool_names = {tool.name for tool in context.tools}
        if "CHILD EXPLICIT REPORT TASK" in text and "report_agent_result" in tool_names:
            self.child_context_count += 1
            assert tool_names >= {"report_agent_phase", "report_agent_result"}
            assert "spawn_agent" not in tool_names
            async for event in _tool_reply(
                event_context,
                tool_call_id="tool:child-phase",
                name="report_agent_phase",
                arguments={"phase": "verifying", "message": "Preparing final report."},
            ):
                yield event
            async for event in _tool_reply(
                event_context,
                tool_call_id="tool:child-result",
                name="report_agent_result",
                arguments={
                    "summary": "explicit child result",
                    "output_preview": "explicit child result with evidence",
                },
            ):
                yield event
        elif '"summary": "explicit child result"' in text:
            async for event in _text_reply(event_context, "parent got explicit report"):
                yield event
        elif '"status": "started"' in text:
            subagent_run_id = _extract_subagent_run_id(text)
            async for event in _tool_reply(
                event_context,
                tool_call_id="tool:wait-explicit-child",
                name="wait_agent",
                arguments={"subagent_run_id": subagent_run_id, "timeout_seconds": 1},
            ):
                yield event
        else:
            async for event in _tool_reply(
                event_context,
                tool_call_id="tool:spawn-explicit-child",
                name="spawn_agent",
                arguments={
                    "task": "CHILD EXPLICIT REPORT TASK",
                    "label": "explicit-worker",
                    "role": "worker",
                    "context": "isolated",
                },
            ):
                yield event


def test_child_report_agent_result_finishes_child_without_followup_model_call(
    tmp_path,
) -> None:
    transport = _ExplicitReportChildTransport()
    registry = LLMTransportRegistry()
    registry.register(transport)
    parent_session = in_memory_runtime_session(
        tmp_path, runtime_session_id="runtime:parent"
    )
    agent = AgentRuntime(
        runtime_session=parent_session,
        llm_runtime=LLMRuntime(
            config=_subagent_test_llm_config(
                api_key="sk-test",
                base_url="https://example.test/v1",
                pro_model="pro",
                flash_model="flash",
                api="explicit-report-child",
            ),
            registry=registry,
        ),
        capability_runtime=CapabilityRuntime(),
    )

    result = asyncio.run(run_agent_task(agent, "Parent: delegate explicit report."))

    assert result.status is LoopStatus.FINISHED
    assert result.final_text == "parent got explicit report"
    assert transport.child_context_count == 1
    assert agent.subagent_runtime is not None
    graph = agent.subagent_runtime.graph()
    assert graph.nodes[0].status == "completed"
    assert graph.nodes[0].phase == "verifying"
    assert graph.nodes[0].consumed_by_wait is True
    waited_result = agent.subagent_runtime.result_for_run(
        graph.nodes[0].subagent_run_id
    )
    assert waited_result is not None
    assert waited_result.result_source == "explicit"
    parent_events = parent_session.event_log.iter()
    submitted_index = next(
        index
        for index, event in enumerate(parent_events)
        if isinstance(event, SubagentResultSubmittedEvent)
    )
    completed_index = next(
        index
        for index, event in enumerate(parent_events)
        if isinstance(event, SubagentRunCompletedEvent)
    )
    assert submitted_index < completed_index
    wait_edge = next(
        event
        for event in parent_events
        if isinstance(event, SubagentEdgeRecordedEvent) and event.edge_kind == "wait"
    )
    assert wait_edge.result_id == waited_result.result_id


class _BackgroundSubagentResultTransport:
    api = "background-subagent-scripted"
    binding_id = "test.background-subagent-scripted"
    contract_version = "v1"

    def __init__(self) -> None:
        self.contexts: list[LLMContext] = []

    async def stream(
        self,
        *,
        call,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[AgentEvent]:
        del call
        self.contexts.append(context)
        text = _context_text(context)
        assert "## Subagent Results" in text
        assert "background child summary" in text
        assert "[context timing: freshness=subagent_result;" in text
        async for event in _text_reply(
            event_context, "parent used background child result"
        ):
            yield event


def test_background_subagent_result_enters_parent_context_and_marks_delivered(
    tmp_path,
) -> None:
    parent, _locator, _child_logs, subagent_runtime = _runtime(tmp_path)

    async def seed_child_result() -> None:
        seed_context = EventContext(
            run_id="run:seed", turn_id="turn:seed", reply_id="reply:seed"
        )
        await _emit_parent_run_start(
            parent,
            event_context=seed_context,
        )
        subagent = await subagent_runtime.spawn_fake(
            task="background child task",
            event_context=seed_context,
        )
        await subagent_runtime.complete_fake(
            subagent.subagent_run_id,
            summary="background child summary",
            event_context=seed_context,
        )

    asyncio.run(seed_child_result())

    transport = _BackgroundSubagentResultTransport()
    registry = LLMTransportRegistry()
    registry.register(transport)
    agent = AgentRuntime(
        runtime_session=parent,
        llm_runtime=LLMRuntime(
            config=_subagent_test_llm_config(
                api_key="sk-test",
                base_url="https://example.test/v1",
                pro_model="pro",
                flash_model="flash",
                api="background-subagent-scripted",
            ),
            registry=registry,
        ),
        capability_runtime=CapabilityRuntime(),
        subagent_runtime=subagent_runtime,
    )

    result = asyncio.run(run_agent_task(agent, "Use any completed subagent result."))

    assert result.status is LoopStatus.FINISHED
    assert result.final_text == "parent used background child result"
    delivered = [
        event
        for event in parent.event_log.iter()
        if isinstance(event, SubagentResultDeliveredEvent)
    ]
    assert len(delivered) == 1
    assert delivered[0].context_id == transport.contexts[0].context_id
    assert delivered[0].model_call_index == transport.contexts[0].model_call_index
    assert delivered[0].section_id == "subagent:results"
    completed = next(
        event
        for event in parent.event_log.iter()
        if isinstance(event, SubagentRunCompletedEvent)
    )
    compiled = next(
        event
        for event in parent.event_log.iter(run_id=result.state.run_id)
        if isinstance(event, ContextCompiledEvent)
    )
    subagent_section = next(
        section for section in compiled.sections if section["id"] == "subagent:results"
    )
    assert subagent_section["metadata"]["source_timing"]["freshness"] == (
        "subagent_result"
    )
    assert subagent_section["metadata"]["source_timing"]["clock_source"] == (
        "event_created_at"
    )
    assert (
        subagent_section["metadata"]["source_timing"]["source_sequence_start"]
        == completed.sequence
    )
    graph = subagent_runtime.graph()
    assert graph.nodes[0].delivered is True
    assert graph.nodes[0].consumed_by_wait is False


class _MismatchedModelStartBackgroundTransport(_BackgroundSubagentResultTransport):
    api = "background-subagent-mismatched-start"
    binding_id = "test.background-subagent-mismatched-start"

    async def stream(
        self,
        *,
        call,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[AgentEvent]:
        self.contexts.append(context)
        text = _context_text(context)
        yield ModelCallStartEvent(
            **event_context.event_fields(),
            resolved_call=call.fact,
            context_id="context:not-this-call",
            model_call_index=(context.model_call_index or 0) + 100,
        )
        assert "## Subagent Results" in text
        assert "background child summary" in text
        async for event in _text_reply(
            event_context, "parent saw but delivery metadata mismatched"
        ):
            yield event


def test_transport_cannot_forge_second_model_start_or_duplicate_background_delivery(
    tmp_path,
) -> None:
    parent, _locator, _child_logs, subagent_runtime = _runtime(tmp_path)

    async def seed_child_result() -> None:
        seed_context = EventContext(
            run_id="run:seed", turn_id="turn:seed", reply_id="reply:seed"
        )
        await _emit_parent_run_start(
            parent,
            event_context=seed_context,
        )
        subagent = await subagent_runtime.spawn_fake(
            task="background child task",
            event_context=seed_context,
        )
        await subagent_runtime.complete_fake(
            subagent.subagent_run_id,
            summary="background child summary",
            event_context=seed_context,
        )

    asyncio.run(seed_child_result())

    transport = _MismatchedModelStartBackgroundTransport()
    registry = LLMTransportRegistry()
    registry.register(transport)
    agent = AgentRuntime(
        runtime_session=parent,
        llm_runtime=LLMRuntime(
            config=_subagent_test_llm_config(
                api_key="sk-test",
                base_url="https://example.test/v1",
                pro_model="pro",
                flash_model="flash",
                api="background-subagent-mismatched-start",
            ),
            registry=registry,
        ),
        capability_runtime=CapabilityRuntime(),
        subagent_runtime=subagent_runtime,
    )

    result = asyncio.run(run_agent_task(agent, "Use any completed subagent result."))

    assert result.status is LoopStatus.FAILED
    delivered = [
        event
        for event in parent.event_log.iter()
        if isinstance(event, SubagentResultDeliveredEvent)
    ]
    assert len(delivered) == 1
    assert delivered[0].context_id != "context:not-this-call"
    assert delivered[0].model_call_index != 100
    assert any(
        isinstance(event, ModelCallEndEvent)
        and event.outcome == "provider_error"
        for event in parent.event_log.iter()
    )
    graph = subagent_runtime.graph()
    assert graph.nodes[0].delivered is True
    assert graph.nodes[0].consumed_by_wait is False


async def _text_reply(
    event_context: EventContext, text: str
) -> AsyncIterator[AgentEvent]:
    block_id = f"text:{event_context.run_id}"
    yield TextBlockStartEvent(**event_context.event_fields(), block_id=block_id)
    yield TextBlockDeltaEvent(
        **event_context.event_fields(), block_id=block_id, delta=text
    )
    yield TextBlockEndEvent(**event_context.event_fields(), block_id=block_id)


async def _tool_reply(
    event_context: EventContext,
    *,
    tool_call_id: str,
    name: str,
    arguments: dict[str, object],
) -> AsyncIterator[AgentEvent]:
    yield ToolCallStartEvent(
        **event_context.event_fields(),
        tool_call_id=tool_call_id,
        tool_call_name=name,
    )
    yield ToolCallDeltaEvent(
        **event_context.event_fields(),
        tool_call_id=tool_call_id,
        delta=json.dumps(arguments),
    )
    yield ToolCallEndEvent(**event_context.event_fields(), tool_call_id=tool_call_id)


def _context_text(context: LLMContext) -> str:
    parts = []
    if context.system_prompt:
        parts.append(context.system_prompt)
    parts.extend(part for message in context.messages for part in message.content)
    return "\n".join(parts)


def _extract_subagent_run_id(text: str) -> str:
    match = re.search(r'"subagent_run_id":\s*"([^"]+)"', text)
    assert match is not None
    return match.group(1)
