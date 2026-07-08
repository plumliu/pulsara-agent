import asyncio
import json
import re
from collections.abc import AsyncIterator

import pytest

from tests.support.runtime_session import in_memory_runtime_session

from pulsara_agent.event import (
    AgentEvent,
    CapabilityGateDecisionEvent,
    CustomEvent,
    EventContext,
    ModelCallEndEvent,
    ModelCallStartEvent,
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
from pulsara_agent.capability.types import CapabilityResolveContext
from pulsara_agent.host.identity import HostWorkspaceInput, resolve_workspace
from pulsara_agent.host.session import HostSession
from pulsara_agent.llm import LLMConfig, LLMRuntime, ModelProfile
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.message import ToolResultState
from pulsara_agent.runtime.subagent import (
    InMemoryEventLogLocator,
    SubagentBudget,
    SubagentLimitExceeded,
    SubagentRuntime,
)
from pulsara_agent.runtime import AgentRuntime, LoopStatus
from pulsara_agent.runtime.permission import PermissionMode, preset_to_policy
from pulsara_agent.runtime.wiring import AgentRuntimeWiring, build_in_memory_runtime_wiring
from pulsara_agent.runtime.transcript import rebuild_prior_messages
from pulsara_agent.tools.base import ToolCall, ToolRuntimeContext
from pulsara_agent.tools.builtins.subagent import (
    CreateAgentTasksTool,
    ListAgentsTool,
    ReportAgentResultTool,
    SpawnAgentTool,
    StopAgentTaskTool,
    WaitAgentTasksTool,
)


CTX = EventContext(run_id="run:parent", turn_id="turn:parent", reply_id="reply:parent")


def _runtime(tmp_path, *, budget: SubagentBudget | None = None, child_runner=None):
    parent = in_memory_runtime_session(tmp_path, runtime_session_id="runtime:parent")
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


def test_subagent_graph_events_are_parent_stream_and_task_is_artifact_backed(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)

    async def run() -> None:
        subagent = await runtime.spawn_fake(
            task="Implement the isolated worker task.\n" * 30,
            event_context=CTX,
            label="worker-a",
            parent_context_id="context:parent:1",
            parent_model_call_index=2,
            spawning_tool_call_id="tool:spawn",
            spawning_tool_name="spawn_agent",
        )
        events = parent.event_log.iter()
        assert [type(event) for event in events] == [SubagentRunStartedEvent, SubagentMessageSentEvent]

        started = events[0]
        assert isinstance(started, SubagentRunStartedEvent)
        assert started.type == "SUBAGENT_RUN_STARTED"
        assert started.parent_context_id == "context:parent:1"
        assert started.parent_model_call_index == 2
        assert started.spawning_tool_call_id == "tool:spawn"
        assert started.child_runtime_session_id == subagent.child_runtime_session_id
        assert len(started.task_preview) <= 500

        task_artifact_id = f"{subagent.subagent_run_id}:task"
        assert parent.archive.get_text(task_artifact_id, session_id=parent.runtime_session_id).startswith(
            "Implement the isolated worker task."
        )

        graph = runtime.graph()
        assert len(graph.nodes) == 1
        assert graph.nodes[0].status == "running"
        assert len(graph.edges) == 1
        assert graph.edges[0].edge_kind == "spawn"
        assert graph.edges[0].payload_artifact_id == task_artifact_id

    asyncio.run(run())


def test_child_raw_events_get_subagent_metadata_at_runtime_session_boundary(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)

    async def run() -> None:
        subagent = await runtime.spawn_fake(task="child task", event_context=CTX)
        child = runtime.child_runtime_session(subagent.subagent_run_id)
        child_ctx = EventContext(run_id="run:child", turn_id="turn:child", reply_id="reply:child")

        stored = await child.emit(TextBlockDeltaEvent(**child_ctx.event_fields(), block_id="text:1", delta="hello"))

        assert stored.metadata["subagent"]["subagent_run_id"] == subagent.subagent_run_id
        assert stored.metadata["subagent"]["parent_runtime_session_id"] == parent.runtime_session_id
        assert stored.metadata["subagent"]["parent_run_id"] == CTX.run_id
        assert stored.metadata["subagent"]["capability_profile_id"] == subagent.capability_profile.profile_id
        assert all(not isinstance(event, TextBlockDeltaEvent) for event in parent.event_log.iter())

    asyncio.run(run())


def test_child_publish_stored_event_requires_boundary_metadata(tmp_path) -> None:
    _parent, _locator, _child_logs, runtime = _runtime(tmp_path)

    async def run() -> None:
        subagent = await runtime.spawn_fake(task="child task", event_context=CTX)
        child = runtime.child_runtime_session(subagent.subagent_run_id)
        child_ctx = EventContext(run_id="run:child", turn_id="turn:child", reply_id="reply:child")
        directly_appended = child.event_log.append(
            TextBlockDeltaEvent(**child_ctx.event_fields(), block_id="text:1", delta="missing metadata")
        )

        with pytest.raises(ValueError, match="default metadata"):
            child.publish_stored_event(directly_appended)

    asyncio.run(run())


def test_child_publish_stored_event_requires_nested_subagent_metadata_values(tmp_path) -> None:
    _parent, _locator, _child_logs, runtime = _runtime(tmp_path)

    async def run() -> None:
        subagent = await runtime.spawn_fake(task="child task", event_context=CTX)
        child = runtime.child_runtime_session(subagent.subagent_run_id)
        child_ctx = EventContext(run_id="run:child", turn_id="turn:child", reply_id="reply:child")
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
        result = await runtime.complete_fake(subagent.subagent_run_id, summary="child finished", event_context=CTX)
        waited = await runtime.wait_result(
            subagent.subagent_run_id,
            event_context=EventContext(run_id="run:wait", turn_id="turn:wait", reply_id="reply:wait"),
            returned_to_tool_call_id="tool:wait",
            source_context_id="context:wait",
            source_model_call_index=3,
        )

        assert waited is result
        events = parent.event_log.iter()
        assert any(isinstance(event, SubagentRunCompletedEvent) for event in events)
        assert any(isinstance(event, SubagentEdgeRecordedEvent) for event in events)
        assert not any(isinstance(event, SubagentResultDeliveredEvent) for event in events)

        wait_edge = next(event for event in events if isinstance(event, SubagentEdgeRecordedEvent))
        assert wait_edge.edge_kind == "wait"
        assert wait_edge.result_id == result.result_id
        assert wait_edge.result_artifact_id == result.final_message_artifact_id
        assert wait_edge.returned_to_tool_call_id == "tool:wait"

        graph = runtime.graph()
        assert graph.nodes[0].status == "completed"
        assert graph.nodes[0].consumed_by_wait is True
        assert graph.nodes[0].delivered is False

    asyncio.run(run())


def test_spawn_agent_tool_rejects_invalid_role_and_context_before_persisting(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    tool = SpawnAgentTool(runtime)

    async def run() -> None:
        result = await tool.execute_async(
            ToolCall(
                id="tool:spawn",
                name="spawn_agent",
                arguments={"task": "child task", "role": "wizard", "context": "telepathic"},
            ),
            runtime_context=ToolRuntimeContext(runtime_session_id=parent.runtime_session_id, event_context=CTX),
        )
        assert result.status.value == "error"
        payload = json.loads(result.output)
        assert payload["status"] == "error"
        assert "role must be one of" in payload["error"]

    asyncio.run(run())

    assert not any(isinstance(event, SubagentRunStartedEvent) for event in parent.event_log.iter())


def test_list_agents_tool_returns_bounded_run_only_projection(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    tool = ListAgentsTool(runtime)

    async def run() -> None:
        subagent = await runtime.spawn_fake(task="child task", event_context=CTX, label="worker-a")
        await runtime.complete_fake(subagent.subagent_run_id, summary="child finished", event_context=CTX)

        result = await tool.execute_async(
            ToolCall(id="tool:list", name="list_agents", arguments={"include_edges": True}),
            runtime_context=ToolRuntimeContext(runtime_session_id=parent.runtime_session_id, event_context=CTX),
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


def test_subagent_task_can_exist_without_child_run_and_projects_to_list_agents(tmp_path) -> None:
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
        assert [type(event) for event in parent.event_log.iter()] == [SubagentTaskCreatedEvent]
        graph = runtime.graph()
        assert len(graph.tasks) == 1
        assert graph.tasks[0].task_id == task.task_id
        assert graph.tasks[0].status == "created"
        assert graph.tasks[0].current_run_id is None
        assert graph.nodes == ()

        result = await tool.execute_async(
            ToolCall(id="tool:list", name="list_agents", arguments={}),
            runtime_context=ToolRuntimeContext(runtime_session_id=parent.runtime_session_id, event_context=CTX),
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


def test_subagent_task_start_links_child_run_without_duplicate_list_item(tmp_path) -> None:
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
        event_types = [type(event) for event in parent.event_log.iter()]
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
            runtime_context=ToolRuntimeContext(runtime_session_id=parent.runtime_session_id, event_context=CTX),
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


def test_subagent_task_completion_updates_task_projection(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)

    async def run() -> None:
        task = await runtime.create_task(
            objective="Summarize completed work.",
            event_context=CTX,
            profile_id="research_worker",
        )
        subagent = await runtime.start_task(task.task_id, event_context=CTX, spawn_initiator_id="tool:create")
        result = await runtime.complete_fake(
            subagent.subagent_run_id,
            summary="worker result",
            event_context=CTX,
        )

        assert runtime.tasks[0].status == "completed"
        assert runtime.tasks[0].result_id == result.result_id
        assert runtime.runs[0].result_source == "inferred"
        assert any(isinstance(event, SubagentTaskCompletedEvent) for event in parent.event_log.iter())
        graph = runtime.graph()
        assert graph.tasks[0].status == "completed"
        assert graph.tasks[0].result_id == result.result_id
        assert graph.tasks[0].primary_result_artifact_id == result.final_message_artifact_id

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
        subagent = await runtime.start_task(task.task_id, event_context=CTX, spawn_initiator_id="tool:create")

        await runtime.report_phase(
            subagent.subagent_run_id,
            phase="investigating",
            message="Reading nearby tests.",
            event_context=CTX,
            source_tool_call_id="tool:phase",
        )

        phase_event = next(event for event in parent.event_log.iter() if isinstance(event, SubagentPhaseReportedEvent))
        assert phase_event.task_id == task.task_id
        assert phase_event.source_tool_call_id == "tool:phase"
        graph = runtime.graph()
        assert graph.nodes[0].phase == "investigating"
        assert graph.tasks[0].phase == "investigating"
        result = await tool.execute_async(
            ToolCall(id="tool:list", name="list_agents", arguments={}),
            runtime_context=ToolRuntimeContext(runtime_session_id=parent.runtime_session_id, event_context=CTX),
        )
        payload = json.loads(result.output)
        assert payload["items"][0]["phase"] == "investigating"

    asyncio.run(run())


def test_report_agent_result_submits_explicit_result_before_completion(tmp_path) -> None:
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
                runtime_session_id=runtime.child_runtime_session(subagent.subagent_run_id).runtime_session_id,
                event_context=CTX,
            ),
        )
        assert submitted.status is ToolResultState.SUCCESS
        submitted_event = next(event for event in parent.event_log.iter() if isinstance(event, SubagentResultSubmittedEvent))
        assert submitted_event.result_source == "explicit"
        assert submitted_event.source_tool_call_id == "tool:report-result"
        assert not any(isinstance(event, SubagentRunCompletedEvent) for event in parent.event_log.iter())

        result = await runtime.complete_submitted_result(subagent.subagent_run_id, event_context=CTX)

        events = parent.event_log.iter()
        submitted_index = next(index for index, event in enumerate(events) if isinstance(event, SubagentResultSubmittedEvent))
        completed_index = next(index for index, event in enumerate(events) if isinstance(event, SubagentRunCompletedEvent))
        assert submitted_index < completed_index
        completed_event = events[completed_index]
        assert isinstance(completed_event, SubagentRunCompletedEvent)
        assert completed_event.result_id == submitted_event.result_id == result.result_id
        assert runtime.runs[0].result_source == "explicit"
        waited = await runtime.wait_result(
            subagent.subagent_run_id,
            event_context=CTX,
            returned_to_tool_call_id="tool:wait",
        )
        assert waited.summary == "explicit child result"
        assert waited.result_source == "explicit"

    asyncio.run(run())


def test_builtin_profiles_compute_child_tool_boundaries(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    parent.subagent_runtime = runtime
    executor = parent.create_tool_executor()
    exposure = CapabilityRuntime().resolve_for_turn(
        CapabilityResolveContext(
            workspace_root=tmp_path,
            workspace_kind="project",
            memory_domain=None,
            available_tool_names=frozenset(executor.registry.names()),
            user_input="profile test",
        ),
        tool_registry=executor.registry,
    )
    runtime.refresh_parent_capability_snapshot(
        exposure=exposure,
        permission_mode="bypass",
        permission_policy={"mode": "bypass"},
    )

    async def run() -> None:
        profiles = {}
        for profile_name in ("research_worker", "review_worker", "verification_worker"):
            task = await runtime.create_task(
                objective=f"{profile_name} task",
                event_context=CTX,
                profile_id=profile_name,
            )
            subagent = await runtime.start_task(task.task_id, event_context=CTX, spawn_initiator_id="tool:create")
            profiles[profile_name] = set(subagent.capability_profile.allowed_tool_names)

        for profile_name in ("research_worker", "review_worker"):
            assert {"read_file", "search_files", "artifact_read"} <= profiles[profile_name]
            assert {"report_agent_phase", "report_agent_result"} <= profiles[profile_name]
            assert not ({"terminal", "terminal_process", "write_file", "edit_file"} & profiles[profile_name])
            assert "spawn_agent" not in profiles[profile_name]
            assert not any(name.startswith("memory_") or name.startswith("remember_") for name in profiles[profile_name])

        assert {"read_file", "search_files", "artifact_read", "terminal", "terminal_process"} <= profiles[
            "verification_worker"
        ]
        assert {"write_file", "edit_file", "spawn_agent"} & profiles["verification_worker"] == set()
        assert {"report_agent_phase", "report_agent_result"} <= profiles["verification_worker"]

    asyncio.run(run())


def test_create_agent_tasks_starts_independent_batch(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    parent.subagent_runtime = runtime
    executor = parent.create_tool_executor()
    exposure = CapabilityRuntime().resolve_for_turn(
        CapabilityResolveContext(
            workspace_root=tmp_path,
            workspace_kind="project",
            memory_domain=None,
            available_tool_names=frozenset(executor.registry.names()),
            user_input="create tasks",
        ),
        tool_registry=executor.registry,
    )
    runtime.refresh_parent_capability_snapshot(
        exposure=exposure,
        permission_mode="bypass",
        permission_policy={"mode": "bypass"},
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


def test_create_agent_tasks_materializes_batch_with_event_log_extend(tmp_path, monkeypatch) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    tool = CreateAgentTasksTool(runtime)
    extend_calls = 0
    original_event_log = parent.event_log

    class BatchOnlyEventLog:
        def append(self, event):
            raise AssertionError("create_agent_tasks must not append batch facts one by one")

        def extend(self, events):
            nonlocal extend_calls
            extend_calls += 1
            return original_event_log.extend(events)

        def iter(self):
            return original_event_log.iter()

    monkeypatch.setattr(parent, "event_log", BatchOnlyEventLog())

    async def run() -> None:
        result = await tool.execute_async(
            ToolCall(
                id="tool:create-tasks",
                name="create_agent_tasks",
                arguments={
                    "tasks": [
                        {"task_key": "review", "profile": "review_worker", "task": "Review"},
                        {"task_key": "verify", "profile": "verification_worker", "task": "Verify"},
                    ]
                },
            ),
            runtime_context=ToolRuntimeContext(runtime_session_id=parent.runtime_session_id, event_context=CTX),
        )

        assert result.status is ToolResultState.SUCCESS
        assert extend_calls == 1
        payload = json.loads(result.output)
        assert payload["status"] == "accepted"
        assert payload["started_count"] == 2

    asyncio.run(run())


def test_create_agent_tasks_rejects_dependencies_without_persisting_tasks(tmp_path) -> None:
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
            runtime_context=ToolRuntimeContext(runtime_session_id=parent.runtime_session_id, event_context=CTX),
        )

        assert result.status is ToolResultState.ERROR
        payload = json.loads(result.output)
        assert payload["failed_stage"] == "preflight"
        assert payload["error_code"] == "subagent_task_batch_preflight_failed"
        assert payload["failed_task_keys"] == ["review"]
        assert not any(isinstance(event, SubagentTaskCreatedEvent) for event in parent.event_log.iter())
        assert runtime.tasks == ()
        assert runtime.runs == ()

    asyncio.run(run())


def test_create_agent_tasks_dependency_waits_then_starts_after_upstream_completion(tmp_path) -> None:
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
            runtime_context=ToolRuntimeContext(runtime_session_id=parent.runtime_session_id, event_context=CTX),
        )
        payload = json.loads(created.output)
        review_task = next(item for item in payload["tasks"] if item["task_key"] == "review")
        verify_task = next(item for item in payload["tasks"] if item["task_key"] == "verify")
        assert review_task["status"] == "running"
        assert verify_task["status"] == "waiting_dependency"
        assert verify_task["subagent_run_id"] is None
        assert len(runtime.runs) == 1
        review_run_id = review_task["subagent_run_id"]

        await runtime.complete_fake(review_run_id, summary="review done", event_context=CTX)

        graph = runtime.graph()
        by_key = {task.task_key: task for task in graph.tasks}
        assert by_key["review"].status == "completed"
        assert by_key["verify"].status == "running"
        assert by_key["verify"].current_run_id is not None
        assert len(runtime.runs) == 2

    asyncio.run(run())


def test_create_agent_tasks_post_commit_failure_terminalizes_materialized_batch(tmp_path, monkeypatch) -> None:
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
            runtime_context=ToolRuntimeContext(runtime_session_id=parent.runtime_session_id, event_context=CTX),
        )

        assert result.status is ToolResultState.ERROR
        payload = json.loads(result.output)
        assert payload["failed_stage"] == "post_commit_start"
        assert payload["error_code"] == "subagent_task_batch_start_failed"
        assert "tasks" not in payload

        graph = runtime.graph()
        assert {task.status for task in graph.tasks} == {"cancelled"}
        assert all(not task.current_run_id or task.status == "cancelled" for task in graph.tasks)
        assert {run.status for run in runtime.runs} == {"cancelled"}
        task_cancelled_events = [
            event for event in parent.event_log.iter() if isinstance(event, SubagentTaskCancelledEvent)
        ]
        assert len(task_cancelled_events) == 3

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
                        {"task_key": "b", "profile": "review_worker", "task": "B", "depends_on": ["a"]},
                    ]
                },
            ),
            runtime_context=ToolRuntimeContext(runtime_session_id=parent.runtime_session_id, event_context=CTX),
        )
        payload = json.loads(created.output)
        a_run_id = next(item for item in payload["tasks"] if item["task_key"] == "a")["subagent_run_id"]

        await runtime.fail(a_run_id, reason_code="test_failure", reason_message="A failed", event_context=CTX)

        graph = runtime.graph()
        by_key = {task.task_key: task for task in graph.tasks}
        assert by_key["a"].status == "failed"
        assert by_key["b"].status == "blocked_dependency_failed"
        assert by_key["b"].current_run_id is None
        assert by_key["b"].blocked_by_task_ids == (by_key["a"].task_id,)
        assert by_key["b"].dependency_status_snapshot == {by_key["a"].task_id: "failed"}
        assert by_key["b"].dependency_terminal_event_ids[by_key["a"].task_id].startswith("event_sequence:")
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
                        {"task_key": "b", "profile": "review_worker", "task": "B", "depends_on": ["a"]},
                        {"task_key": "c", "profile": "review_worker", "task": "C", "depends_on": ["b"]},
                    ]
                },
            ),
            runtime_context=ToolRuntimeContext(runtime_session_id=parent.runtime_session_id, event_context=CTX),
        )
        payload = json.loads(created.output)
        a_run_id = next(item for item in payload["tasks"] if item["task_key"] == "a")["subagent_run_id"]

        await runtime.fail(a_run_id, reason_code="test_failure", reason_message="A failed", event_context=CTX)

        graph = runtime.graph()
        by_key = {task.task_key: task for task in graph.tasks}
        assert by_key["a"].status == "failed"
        assert by_key["b"].status == "blocked_dependency_failed"
        assert by_key["c"].status == "blocked_dependency_failed"
        assert by_key["c"].blocked_by_task_ids == (by_key["b"].task_id,)
        assert by_key["c"].dependency_status_snapshot == {by_key["b"].task_id: "blocked_dependency_failed"}
        assert by_key["c"].dependency_terminal_event_ids[by_key["b"].task_id].startswith("event_sequence:")

        waited = await runtime.wait_tasks(
            (by_key["a"].task_id, by_key["b"].task_id, by_key["c"].task_id),
            event_context=CTX,
            consumer_tool_call_id="tool:wait-tasks",
            settle="all",
            timeout_seconds=0,
        )
        assert {item["status"] for item in waited} == {"failed", "blocked_dependency_failed"}
        assert len(waited) == 3

    asyncio.run(run())


def test_create_agent_tasks_blocks_transitive_dependency_on_existing_failed_task(tmp_path) -> None:
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
                        {"task_key": "c", "profile": "review_worker", "task": "C", "depends_on": ["b"]},
                    ]
                },
            ),
            runtime_context=ToolRuntimeContext(runtime_session_id=parent.runtime_session_id, event_context=CTX),
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
        assert by_key["b"].dependency_terminal_event_ids[upstream.task_id].startswith("event_sequence:")
        assert by_key["c"].blocked_by_task_ids == (by_key["b"].task_id,)
        assert by_key["c"].dependency_status_snapshot == {by_key["b"].task_id: "blocked_dependency_failed"}
        assert by_key["c"].dependency_terminal_event_ids[by_key["b"].task_id].startswith(
            f"subagent_task_terminal:{by_key['b'].task_id}:"
        )
        assert by_key["c"].dependency_generation is not None

    asyncio.run(run())


def test_create_agent_tasks_rejects_dependency_cycle_without_persisting_tasks(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)
    tool = CreateAgentTasksTool(runtime)

    async def run() -> None:
        result = await tool.execute_async(
            ToolCall(
                id="tool:create-tasks",
                name="create_agent_tasks",
                arguments={
                    "tasks": [
                        {"task_key": "a", "profile": "review_worker", "task": "A", "depends_on": ["b"]},
                        {"task_key": "b", "profile": "review_worker", "task": "B", "depends_on": ["a"]},
                    ]
                },
            ),
            runtime_context=ToolRuntimeContext(runtime_session_id=parent.runtime_session_id, event_context=CTX),
        )

        assert result.status is ToolResultState.ERROR
        payload = json.loads(result.output)
        assert payload["failed_stage"] == "preflight"
        assert "dependency cycle" in payload["diagnostics"][0]["message"]
        assert runtime.tasks == ()
        assert not any(isinstance(event, SubagentTaskCreatedEvent) for event in parent.event_log.iter())

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
            subagent = await runtime.start_task(task.task_id, event_context=CTX, spawn_initiator_id="tool:create")
            await runtime.complete_fake(subagent.subagent_run_id, summary=f"result {key}", event_context=CTX)
            tasks.append(task)

        result = await wait_tool.execute_async(
            ToolCall(
                id="tool:wait-tasks",
                name="wait_agent_tasks",
                arguments={"task_ids": [task.task_id for task in tasks], "settle": "all"},
            ),
            runtime_context=ToolRuntimeContext(runtime_session_id=parent.runtime_session_id, event_context=CTX),
        )

        assert result.status is ToolResultState.SUCCESS
        payload = json.loads(result.output)
        assert payload["returned_count"] == 2
        assert {item["summary"] for item in payload["results"]} == {"result a", "result b"}
        consumed = [event for event in parent.event_log.iter() if isinstance(event, SubagentResultConsumedEvent)]
        assert len(consumed) == 2
        assert {event.kind for event in consumed} == {"wait_task"}
        graph = runtime.graph()
        assert all(task.consumed_by_wait for task in graph.tasks)

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
        done_run = await runtime.start_task(done_task.task_id, event_context=CTX, spawn_initiator_id="tool:create")
        await runtime.complete_fake(done_run.subagent_run_id, summary="done result", event_context=CTX)
        running_task = await runtime.create_task(
            objective="Running task",
            event_context=CTX,
            profile_id="review_worker",
            task_key="running",
        )
        running_run = await runtime.start_task(running_task.task_id, event_context=CTX, spawn_initiator_id="tool:create")

        result = await wait_tool.execute_async(
            ToolCall(
                id="tool:wait-first",
                name="wait_agent_tasks",
                arguments={"task_ids": [done_task.task_id, running_task.task_id], "settle": "first"},
            ),
            runtime_context=ToolRuntimeContext(runtime_session_id=parent.runtime_session_id, event_context=CTX),
        )

        payload = json.loads(result.output)
        assert payload["returned_count"] == 1
        assert payload["results"][0]["task_id"] == done_task.task_id
        assert runtime._runs[running_run.subagent_run_id].status == "running"  # noqa: SLF001 - contract.
        assert not any(
            isinstance(event, SubagentRunCancelledEvent) and event.subagent_run_id == running_run.subagent_run_id
            for event in parent.event_log.iter()
        )

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
        subagent = await runtime.start_task(task.task_id, event_context=CTX, spawn_initiator_id="tool:create")

        result = await stop_tool.execute_async(
            ToolCall(
                id="tool:stop-task",
                name="stop_agent_task",
                arguments={"task_id": task.task_id, "reason": "No longer needed."},
            ),
            runtime_context=ToolRuntimeContext(runtime_session_id=parent.runtime_session_id, event_context=CTX),
        )

        assert result.status is ToolResultState.SUCCESS
        payload = json.loads(result.output)
        assert payload["status"] == "cancelled"
        assert runtime.runs[0].status == "cancelled"
        assert runtime.tasks[0].status == "cancelled"
        assert any(
            isinstance(event, SubagentTaskCancelledEvent) and event.task_id == task.task_id
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
        await runtime.complete_fake(subagent.subagent_run_id, summary="bootstrapped result", event_context=CTX)

    asyncio.run(seed())
    resumed = SubagentRuntime(
        parent_runtime_session=parent,
        child_event_log_factory=lambda runtime_session_id: child_logs.setdefault(runtime_session_id, InMemoryEventLog()),
        event_log_locator=locator,
    )

    async def run() -> None:
        [bootstrapped] = resumed.runs
        result = await resumed.wait_result(
            bootstrapped.subagent_run_id,
            event_context=EventContext(run_id="run:wait", turn_id="turn:wait", reply_id="reply:wait"),
            returned_to_tool_call_id="tool:wait",
        )

        assert result.summary == "bootstrapped result"
        assert resumed.graph().nodes[0].consumed_by_wait is True

    asyncio.run(run())


def test_subagent_spawn_caps_are_enforced(tmp_path) -> None:
    _parent, _locator, _child_logs, runtime = _runtime(
        tmp_path,
        budget=SubagentBudget(max_concurrent_children_per_parent_run=1, max_concurrent_children_per_host_session=1),
    )

    async def run() -> None:
        await runtime.spawn_fake(task="first", event_context=CTX)
        with pytest.raises(SubagentLimitExceeded, match="max_concurrent_children_per_parent_run"):
            await runtime.spawn_fake(task="second", event_context=CTX)

    asyncio.run(run())


def test_repair_dangling_children_fails_bootstrapped_active_run(tmp_path) -> None:
    parent, locator, child_logs, runtime = _runtime(tmp_path)

    async def seed() -> None:
        await runtime.spawn_fake(task="child task", event_context=CTX)

    asyncio.run(seed())
    resumed = SubagentRuntime(
        parent_runtime_session=parent,
        child_event_log_factory=lambda runtime_session_id: child_logs.setdefault(runtime_session_id, InMemoryEventLog()),
        event_log_locator=locator,
    )

    async def run() -> None:
        repaired = await resumed.repair_dangling_children()

        assert len(repaired) == 1
        assert repaired[0].status == "failed"
        failed = [event for event in parent.event_log.iter() if isinstance(event, SubagentRunFailedEvent)]
        assert failed[-1].reason_code == "subagent_dangling_repaired"

    asyncio.run(run())


def test_cancel_stops_running_child_task(tmp_path) -> None:
    started = asyncio.Event()

    async def child_runner(_runtime: SubagentRuntime, _run) -> None:
        started.set()
        await asyncio.Event().wait()

    _parent, _locator, _child_logs, runtime = _runtime(tmp_path, child_runner=child_runner)

    async def run() -> None:
        subagent = await runtime.spawn_agent(task="long child task", event_context=CTX)
        await asyncio.wait_for(started.wait(), timeout=1)
        task = runtime._child_tasks[subagent.subagent_run_id]  # noqa: SLF001 - contract regression.

        await runtime.cancel(subagent.subagent_run_id, event_context=CTX, reason_code="test_cancel")
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
            failed = [event for event in parent.event_log.iter() if isinstance(event, SubagentRunFailedEvent)]
            if failed:
                assert failed[-1].reason_code == "subagent_timeout"
                assert runtime.runs[0].status == "failed"
                return
            await asyncio.sleep(0.001)
        raise AssertionError("subagent timeout did not produce a failure event")

    asyncio.run(run())


def test_cancel_is_idempotent_for_completed_child(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)

    async def run() -> None:
        subagent = await runtime.spawn_fake(task="child task", event_context=CTX)
        await runtime.complete_fake(subagent.subagent_run_id, summary="done", event_context=CTX)

        cancelled = await runtime.cancel(subagent.subagent_run_id, event_context=CTX, reason_code="test_cancel")

        assert cancelled.status == "completed"
        assert not any(isinstance(event, SubagentRunCancelledEvent) for event in parent.event_log.iter())

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
        event for event in parent.event_log.iter() if isinstance(event, SubagentRunCancelledEvent)
    ][-1]
    assert cancellation_event.reason_code == "subagent_bypass_revoked"
    assert cancellation_event.cancelled_by == "runtime"


def test_safety_narrowing_sync_terminalizes_task_and_blocks_dependents(tmp_path) -> None:
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
                        {"task_key": "b", "profile": "review_worker", "task": "B", "depends_on": ["a"]},
                    ]
                },
            ),
            runtime_context=ToolRuntimeContext(runtime_session_id=parent.runtime_session_id, event_context=CTX),
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
    assert by_key["b"].dependency_terminal_event_ids[by_key["a"].task_id].startswith("event_sequence:")

    assert any(isinstance(event, SubagentTaskCancelledEvent) for event in parent.event_log.iter())
    blocked_events = [
        event for event in parent.event_log.iter() if isinstance(event, SubagentTaskBlockedEvent)
    ]
    assert blocked_events[-1].dependency_terminal_event_ids == by_key["b"].dependency_terminal_event_ids


def test_host_session_close_cancels_active_subagents(tmp_path) -> None:
    started = asyncio.Event()
    runtime_wiring = build_in_memory_runtime_wiring(tmp_path)
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
            config=LLMConfig(
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
    )
    session = HostSession(
        host_session_id="host:test",
        conversation_id="conversation:test",
        workspace=resolve_workspace(HostWorkspaceInput(workspace_root=tmp_path, workspace_kind="project")),
        wiring=AgentRuntimeWiring(agent_runtime=agent, runtime_wiring=runtime_wiring),
    )

    async def run() -> None:
        subagent = await subagent_runtime.spawn_agent(task="long child task", event_context=CTX)
        await asyncio.wait_for(started.wait(), timeout=1)
        task = subagent_runtime._child_tasks[subagent.subagent_run_id]  # noqa: SLF001 - contract regression.

        await session.aclose()
        await asyncio.sleep(0)

        assert task.cancelled()
        cancelled = [
            event
            for event in runtime_wiring.runtime_session.event_log.iter()
            if isinstance(event, SubagentRunCancelledEvent)
        ]
        assert cancelled[-1].reason_code == "subagent_host_session_close"
        assert cancelled[-1].cancelled_by == "host_shutdown"

    asyncio.run(run())


def test_host_permission_leaving_bypass_cancels_active_subagents(tmp_path) -> None:
    started = asyncio.Event()
    runtime_wiring = build_in_memory_runtime_wiring(tmp_path)
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
            config=LLMConfig(
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
    )
    session = HostSession(
        host_session_id="host:test",
        conversation_id="conversation:test",
        workspace=resolve_workspace(HostWorkspaceInput(workspace_root=tmp_path, workspace_kind="project")),
        wiring=AgentRuntimeWiring(agent_runtime=agent, runtime_wiring=runtime_wiring),
    )

    async def run() -> None:
        subagent = await subagent_runtime.spawn_agent(task="long child task", event_context=CTX)
        await asyncio.wait_for(started.wait(), timeout=1)
        task = subagent_runtime._child_tasks[subagent.subagent_run_id]  # noqa: SLF001 - contract regression.

        session.set_permission_mode("read-only")
        await asyncio.sleep(0)

        assert task.cancelled()
        cancelled = [
            event
            for event in runtime_wiring.runtime_session.event_log.iter()
            if isinstance(event, SubagentRunCancelledEvent)
        ]
        assert cancelled
        assert cancelled[-1].reason_code == "subagent_bypass_revoked"
        assert cancelled[-1].cancelled_by == "runtime"

    asyncio.run(run())


def test_subagent_events_round_trip_through_agent_event_serialization(tmp_path) -> None:
    _parent, _locator, _child_logs, runtime = _runtime(tmp_path)

    async def run() -> None:
        subagent = await runtime.spawn_fake(task="child task", event_context=CTX)
        await runtime.complete_fake(subagent.subagent_run_id, summary="done", event_context=CTX)

    asyncio.run(run())

    for event in runtime.parent_runtime_session.event_log.iter():
        assert load_agent_event(dump_agent_event(event)) == event


def test_parent_transcript_rebuild_ignores_subagent_graph_events_after_run_end(tmp_path) -> None:
    parent, _locator, _child_logs, runtime = _runtime(tmp_path)

    async def run() -> None:
        from pulsara_agent.event import RunEndEvent, RunStartEvent

        await parent.emit(RunStartEvent(**CTX.event_fields(), user_input_chars=5, metadata={"user_input": "hello"}))
        await parent.emit(RunEndEvent(**CTX.event_fields(), status="finished", stop_reason="final"))
        subagent = await runtime.spawn_fake(task="child task", event_context=CTX)
        await runtime.complete_fake(subagent.subagent_run_id, summary="child done")

    asyncio.run(run())

    messages = rebuild_prior_messages(parent.event_log, archive=parent.archive, session_id=parent.runtime_session_id)
    assert [message.id for message in messages] == [f"user-message:{CTX.run_id}"]


class _SubagentScriptedTransport:
    api = "subagent-scripted"

    def __init__(self) -> None:
        self.contexts: list[LLMContext] = []

    async def stream(
        self,
        *,
        model: ModelProfile,
        context: LLMContext,
        event_context: EventContext,
        options: LLMOptions | None = None,
    ) -> AsyncIterator[AgentEvent]:
        del options
        self.contexts.append(context)
        yield ModelCallStartEvent(
            **event_context.event_fields(),
            model_name=model.id,
            model_role=model.role.value,
            provider=model.provider,
            context_id=context.context_id,
            model_call_index=context.model_call_index,
        )
        text = _context_text(context)
        if "CHILD TASK: summarize the moon" in text:
            async for event in _text_reply(event_context, "child says: the moon is bright"):
                yield event
        elif '"result_id"' in text and "child says: the moon is bright" in text:
            async for event in _text_reply(event_context, "parent received child result"):
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
        yield ModelCallEndEvent(**event_context.event_fields())


class _FinalOnlyTransport:
    api = "final-only"

    async def stream(
        self,
        *,
        model: ModelProfile,
        context: LLMContext,
        event_context: EventContext,
        options: LLMOptions | None = None,
    ) -> AsyncIterator[AgentEvent]:
        del model, options
        yield ModelCallStartEvent(
            **event_context.event_fields(),
            model_name="test",
            model_role="pro",
            provider="test",
            context_id=context.context_id,
            model_call_index=context.model_call_index,
        )
        async for event in _text_reply(event_context, "parent final"):
            yield event
        yield ModelCallEndEvent(**event_context.event_fields())


class _PendingChildTransport:
    api = "pending-child"

    async def stream(
        self,
        *,
        model: ModelProfile,
        context: LLMContext,
        event_context: EventContext,
        options: LLMOptions | None = None,
    ) -> AsyncIterator[AgentEvent]:
        del model, options
        text = _context_text(context)
        yield ModelCallStartEvent(
            **event_context.event_fields(),
            model_name="test",
            model_role="pro",
            provider="test",
            context_id=context.context_id,
            model_call_index=context.model_call_index,
        )
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
                arguments={"question": "Should the child continue?", "allow_free_text": True},
            ):
                yield event
        elif '"status": "started"' in text:
            async for event in _text_reply(event_context, "parent finished after spawn"):
                yield event
        else:
            async for event in _tool_reply(
                event_context,
                tool_call_id="tool:spawn-pending-child",
                name="spawn_agent",
                arguments={"task": "CHILD NEEDS PLAN QUESTION"},
            ):
                yield event
        yield ModelCallEndEvent(**event_context.event_fields())


class _ListAgentsNonBypassTransport:
    api = "list-agents-non-bypass"

    def __init__(self) -> None:
        self.contexts: list[LLMContext] = []

    async def stream(
        self,
        *,
        model: ModelProfile,
        context: LLMContext,
        event_context: EventContext,
        options: LLMOptions | None = None,
    ) -> AsyncIterator[AgentEvent]:
        del model, options
        self.contexts.append(context)
        yield ModelCallStartEvent(
            **event_context.event_fields(),
            model_name="test",
            model_role="pro",
            provider="test",
            context_id=context.context_id,
            model_call_index=context.model_call_index,
        )
        if "subagent_requires_bypass_mode" in _context_text(context):
            async for event in _text_reply(event_context, "list_agents denied outside bypass"):
                yield event
        else:
            async for event in _tool_reply(
                event_context,
                tool_call_id="tool:list-agents",
                name="list_agents",
                arguments={},
            ):
                yield event
        yield ModelCallEndEvent(**event_context.event_fields())


def test_list_agents_is_visible_but_gate_denied_outside_bypass(tmp_path) -> None:
    transport = _ListAgentsNonBypassTransport()
    registry = LLMTransportRegistry()
    registry.register(transport)
    parent_session = in_memory_runtime_session(tmp_path, runtime_session_id="runtime:parent")
    agent = AgentRuntime(
        runtime_session=parent_session,
        llm_runtime=LLMRuntime(
            config=LLMConfig(
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

    result = asyncio.run(agent.run_task("list children"))

    assert result.status is LoopStatus.FINISHED
    assert result.final_text == "list_agents denied outside bypass"
    gate_decision = next(
        event
        for event in parent_session.event_log.iter()
        if isinstance(event, CapabilityGateDecisionEvent) and event.tool_call_id == "tool:list-agents"
    )
    assert gate_decision.tool_name == "list_agents"
    assert gate_decision.decision == "deny"
    assert gate_decision.result_state is ToolResultState.DENIED
    assert gate_decision.reason_code == "subagent_requires_bypass_mode"
    assert gate_decision.permission_category == "subagent_runtime"
    assert any("list_agents" in getattr(tool, "name", "") for tool in agent.tool_executor.registry.all())


def test_agent_runtime_repairs_dangling_subagent_before_turn(tmp_path) -> None:
    parent, locator, child_logs, runtime = _runtime(tmp_path)

    async def seed() -> None:
        await runtime.spawn_fake(task="lost child task", event_context=CTX)

    asyncio.run(seed())
    resumed = SubagentRuntime(
        parent_runtime_session=parent,
        child_event_log_factory=lambda runtime_session_id: child_logs.setdefault(runtime_session_id, InMemoryEventLog()),
        event_log_locator=locator,
    )
    transport = _FinalOnlyTransport()
    registry = LLMTransportRegistry()
    registry.register(transport)
    agent = AgentRuntime(
        runtime_session=parent,
        llm_runtime=LLMRuntime(
            config=LLMConfig(
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

    result = asyncio.run(agent.run_task("resume after lost child"))

    assert result.status is LoopStatus.FINISHED
    failed = [event for event in parent.event_log.iter() if isinstance(event, SubagentRunFailedEvent)]
    assert failed[-1].reason_code == "subagent_dangling_repaired"


def test_child_pending_interaction_fails_closed_without_parent_pending_slot(tmp_path) -> None:
    transport = _PendingChildTransport()
    registry = LLMTransportRegistry()
    registry.register(transport)
    parent_session = in_memory_runtime_session(tmp_path, runtime_session_id="runtime:parent")
    agent = AgentRuntime(
        runtime_session=parent_session,
        llm_runtime=LLMRuntime(
            config=LLMConfig(
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
        result = await agent.run_task("spawn a child that needs approval")
        assert result.status is LoopStatus.FINISHED
        assert result.state.pending_interaction_kind is None
        for _ in range(200):
            if any(
                isinstance(event, SubagentRunFailedEvent)
                and event.reason_code == "subagent_pending_unsupported"
                for event in parent_session.event_log.iter()
            ):
                return
            await asyncio.sleep(0.001)
        raise AssertionError("child pending failure was not recorded")

    asyncio.run(run_parent())


def test_agent_runtime_can_spawn_real_child_runtime_and_wait_result(tmp_path) -> None:
    transport = _SubagentScriptedTransport()
    registry = LLMTransportRegistry()
    registry.register(transport)
    llm_runtime = LLMRuntime(
        config=LLMConfig(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="subagent-scripted",
        ),
        registry=registry,
    )
    parent_session = in_memory_runtime_session(tmp_path, runtime_session_id="runtime:parent")
    agent = AgentRuntime(
        runtime_session=parent_session,
        llm_runtime=llm_runtime,
        capability_runtime=CapabilityRuntime(),
    )

    result = asyncio.run(agent.run_task("Parent: delegate the moon summary."))

    assert result.status is LoopStatus.FINISHED
    assert result.final_text == "parent received child result"
    assert agent.subagent_runtime is not None
    graph = agent.subagent_runtime.graph()
    assert len(graph.nodes) == 1
    assert graph.nodes[0].status == "completed"
    assert graph.nodes[0].consumed_by_wait is True
    assert graph.nodes[0].delivered is False
    assert graph.edges[0].source_tool_call_id == "tool:spawn-child"

    child_session = agent.subagent_runtime.child_runtime_session(graph.nodes[0].subagent_run_id)
    child_events = child_session.event_log.iter()
    assert child_events
    assert all(event.metadata.get("subagent") for event in child_events)
    parent_events = parent_session.event_log.iter()
    assert any(isinstance(event, SubagentRunStartedEvent) for event in parent_events)
    assert any(isinstance(event, SubagentRunCompletedEvent) for event in parent_events)
    assert any(isinstance(event, SubagentEdgeRecordedEvent) for event in parent_events)
    assert not any(isinstance(event, SubagentResultDeliveredEvent) for event in parent_events)
    started_event = next(event for event in parent_events if isinstance(event, SubagentRunStartedEvent))
    wait_edge = next(
        event
        for event in parent_events
        if isinstance(event, SubagentEdgeRecordedEvent) and event.edge_kind == "wait"
    )
    assert started_event.parent_context_id == transport.contexts[0].context_id
    assert started_event.parent_model_call_index == transport.contexts[0].model_call_index
    wait_context = next(context for context in transport.contexts if '"status": "started"' in _context_text(context))
    assert wait_edge.source_context_id == wait_context.context_id
    assert wait_edge.source_model_call_index == wait_context.model_call_index
    allowed_tool_names = set(started_event.capability_profile["allowed_tool_names"])
    assert "spawn_agent" not in allowed_tool_names
    assert not any(name.startswith("memory_") or name.startswith("remember_") for name in allowed_tool_names)
    child_exposures = [
        event.value
        for event in child_events
        if isinstance(event, CustomEvent) and event.name == "capability_exposure_resolved"
    ]
    assert child_exposures
    assert "spawn_agent" not in child_exposures[0]["callable_names"]
    assert "report_agent_result" in child_exposures[0]["callable_names"]


class _ExplicitReportChildTransport:
    api = "explicit-report-child"

    def __init__(self) -> None:
        self.contexts: list[LLMContext] = []
        self.child_context_count = 0

    async def stream(
        self,
        *,
        model: ModelProfile,
        context: LLMContext,
        event_context: EventContext,
        options: LLMOptions | None = None,
    ) -> AsyncIterator[AgentEvent]:
        del options
        self.contexts.append(context)
        text = _context_text(context)
        yield ModelCallStartEvent(
            **event_context.event_fields(),
            model_name=model.id,
            model_role=model.role.value,
            provider=model.provider,
            context_id=context.context_id,
            model_call_index=context.model_call_index,
        )
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
        yield ModelCallEndEvent(**event_context.event_fields())


def test_child_report_agent_result_finishes_child_without_followup_model_call(tmp_path) -> None:
    transport = _ExplicitReportChildTransport()
    registry = LLMTransportRegistry()
    registry.register(transport)
    parent_session = in_memory_runtime_session(tmp_path, runtime_session_id="runtime:parent")
    agent = AgentRuntime(
        runtime_session=parent_session,
        llm_runtime=LLMRuntime(
            config=LLMConfig(
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

    result = asyncio.run(agent.run_task("Parent: delegate explicit report."))

    assert result.status is LoopStatus.FINISHED
    assert result.final_text == "parent got explicit report"
    assert transport.child_context_count == 1
    assert agent.subagent_runtime is not None
    graph = agent.subagent_runtime.graph()
    assert graph.nodes[0].status == "completed"
    assert graph.nodes[0].phase == "verifying"
    assert graph.nodes[0].consumed_by_wait is True
    waited_result = agent.subagent_runtime._results[graph.nodes[0].subagent_run_id]  # noqa: SLF001 - contract.
    assert waited_result.result_source == "explicit"
    parent_events = parent_session.event_log.iter()
    submitted_index = next(index for index, event in enumerate(parent_events) if isinstance(event, SubagentResultSubmittedEvent))
    completed_index = next(index for index, event in enumerate(parent_events) if isinstance(event, SubagentRunCompletedEvent))
    assert submitted_index < completed_index
    wait_edge = next(
        event
        for event in parent_events
        if isinstance(event, SubagentEdgeRecordedEvent) and event.edge_kind == "wait"
    )
    assert wait_edge.result_id == waited_result.result_id


class _BackgroundSubagentResultTransport:
    api = "background-subagent-scripted"

    def __init__(self) -> None:
        self.contexts: list[LLMContext] = []

    async def stream(
        self,
        *,
        model: ModelProfile,
        context: LLMContext,
        event_context: EventContext,
        options: LLMOptions | None = None,
    ) -> AsyncIterator[AgentEvent]:
        del options
        self.contexts.append(context)
        text = _context_text(context)
        yield ModelCallStartEvent(
            **event_context.event_fields(),
            model_name=model.id,
            model_role=model.role.value,
            provider=model.provider,
            context_id=context.context_id,
            model_call_index=context.model_call_index,
        )
        assert "## Subagent Results" in text
        assert "background child summary" in text
        async for event in _text_reply(event_context, "parent used background child result"):
            yield event
        yield ModelCallEndEvent(**event_context.event_fields())


def test_background_subagent_result_enters_parent_context_and_marks_delivered(tmp_path) -> None:
    parent, _locator, _child_logs, subagent_runtime = _runtime(tmp_path)

    async def seed_child_result() -> None:
        subagent = await subagent_runtime.spawn_fake(
            task="background child task",
            event_context=EventContext(run_id="run:seed", turn_id="turn:seed", reply_id="reply:seed"),
        )
        await subagent_runtime.complete_fake(
            subagent.subagent_run_id,
            summary="background child summary",
            event_context=EventContext(run_id="run:seed", turn_id="turn:seed", reply_id="reply:seed"),
        )

    asyncio.run(seed_child_result())

    transport = _BackgroundSubagentResultTransport()
    registry = LLMTransportRegistry()
    registry.register(transport)
    agent = AgentRuntime(
        runtime_session=parent,
        llm_runtime=LLMRuntime(
            config=LLMConfig(
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

    result = asyncio.run(agent.run_task("Use any completed subagent result."))

    assert result.status is LoopStatus.FINISHED
    assert result.final_text == "parent used background child result"
    delivered = [event for event in parent.event_log.iter() if isinstance(event, SubagentResultDeliveredEvent)]
    assert len(delivered) == 1
    assert delivered[0].context_id == transport.contexts[0].context_id
    assert delivered[0].model_call_index == transport.contexts[0].model_call_index
    assert delivered[0].section_id == "subagent:results"
    graph = subagent_runtime.graph()
    assert graph.nodes[0].delivered is True
    assert graph.nodes[0].consumed_by_wait is False


class _MismatchedModelStartBackgroundTransport(_BackgroundSubagentResultTransport):
    api = "background-subagent-mismatched-start"

    async def stream(
        self,
        *,
        model: ModelProfile,
        context: LLMContext,
        event_context: EventContext,
        options: LLMOptions | None = None,
    ) -> AsyncIterator[AgentEvent]:
        del options
        self.contexts.append(context)
        text = _context_text(context)
        yield ModelCallStartEvent(
            **event_context.event_fields(),
            model_name=model.id,
            model_role=model.role.value,
            provider=model.provider,
            context_id="context:not-this-call",
            model_call_index=(context.model_call_index or 0) + 100,
        )
        assert "## Subagent Results" in text
        assert "background child summary" in text
        async for event in _text_reply(event_context, "parent saw but delivery metadata mismatched"):
            yield event
        yield ModelCallEndEvent(**event_context.event_fields())


def test_background_subagent_result_requires_matching_model_start_metadata_for_delivery(tmp_path) -> None:
    parent, _locator, _child_logs, subagent_runtime = _runtime(tmp_path)

    async def seed_child_result() -> None:
        subagent = await subagent_runtime.spawn_fake(
            task="background child task",
            event_context=EventContext(run_id="run:seed", turn_id="turn:seed", reply_id="reply:seed"),
        )
        await subagent_runtime.complete_fake(
            subagent.subagent_run_id,
            summary="background child summary",
            event_context=EventContext(run_id="run:seed", turn_id="turn:seed", reply_id="reply:seed"),
        )

    asyncio.run(seed_child_result())

    transport = _MismatchedModelStartBackgroundTransport()
    registry = LLMTransportRegistry()
    registry.register(transport)
    agent = AgentRuntime(
        runtime_session=parent,
        llm_runtime=LLMRuntime(
            config=LLMConfig(
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

    result = asyncio.run(agent.run_task("Use any completed subagent result."))

    assert result.status is LoopStatus.FINISHED
    assert not any(isinstance(event, SubagentResultDeliveredEvent) for event in parent.event_log.iter())
    graph = subagent_runtime.graph()
    assert graph.nodes[0].delivered is False
    assert graph.nodes[0].consumed_by_wait is False


async def _text_reply(event_context: EventContext, text: str) -> AsyncIterator[AgentEvent]:
    block_id = f"text:{event_context.run_id}"
    yield TextBlockStartEvent(**event_context.event_fields(), block_id=block_id)
    yield TextBlockDeltaEvent(**event_context.event_fields(), block_id=block_id, delta=text)
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
