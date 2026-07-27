from __future__ import annotations

import asyncio

import pytest

from pulsara_agent.event import EventContext
from pulsara_agent.ports.subagent import (
    CreateAgentTasksCommand,
    ListAgentsCommand,
    ReportAgentPhaseCommand,
    ReportAgentResultCommand,
    SpawnAgentCommand,
    StopAgentCommand,
    StopAgentTaskCommand,
    SubagentToolRejectCode,
    SubagentToolRejectedOutcome,
    WaitAgentCommand,
    WaitAgentTasksCommand,
    build_subagent_command_owner,
    build_subagent_tool_command,
)
from pulsara_agent.ports.tool_execution import ToolInvocationOwnerKind
from pulsara_agent.primitives.context import thaw_json
from pulsara_agent.runtime.subagent.runtime import SubagentNotFound
from pulsara_agent.runtime.subagent.tool_port import RuntimeSubagentControlPort
from tests.support.capability import tool_runtime_context


CTX = EventContext(
    run_id="run:subagent-port",
    turn_id="turn:subagent-port",
    reply_id="reply:subagent-port",
)


def _owner(tool_name: str, *, child: bool = False):
    runtime_context = tool_runtime_context(
        runtime_session_id="runtime:subagent-port",
        event_context=CTX,
        owner_kind=(
            ToolInvocationOwnerKind.SUBAGENT_CHILD
            if child
            else ToolInvocationOwnerKind.HOST_MAIN_RUN
        ),
        context_id="context:subagent-port",
        model_call_index=1,
    )
    return build_subagent_command_owner(
        runtime_session_id=runtime_context.runtime_session_id,
        tool_call_id=f"call:{tool_name}",
        tool_name=tool_name,
        event_context=CTX,
        parent_context_id=runtime_context.context_id,
        parent_model_call_index=runtime_context.model_call_index,
        invocation_owner_kind=runtime_context.owner_kind,
        permission=runtime_context.permission,
        bound_child_subagent_run_id=("subagent:child" if child else None),
    )


@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected_type", "child"),
    (
        ("spawn_agent", {"task": "inspect"}, SpawnAgentCommand, False),
        (
            "wait_agent",
            {"subagent_run_id": "subagent:one", "timeout_seconds": 0},
            WaitAgentCommand,
            False,
        ),
        (
            "stop_agent",
            {"subagent_run_id": "subagent:one", "reason": "done"},
            StopAgentCommand,
            False,
        ),
        ("list_agents", {}, ListAgentsCommand, False),
        (
            "create_agent_tasks",
            {
                "tasks": [
                    {
                        "task": "verify",
                        "profile": "verification_worker",
                        "task_key": "verify",
                    }
                ]
            },
            CreateAgentTasksCommand,
            False,
        ),
        (
            "wait_agent_tasks",
            {"task_ids": ["task:one"], "settle": "first"},
            WaitAgentTasksCommand,
            False,
        ),
        (
            "stop_agent_task",
            {"task_id": "task:one"},
            StopAgentTaskCommand,
            False,
        ),
        (
            "report_agent_phase",
            {"phase": "testing", "progress": {"done": [1]}},
            ReportAgentPhaseCommand,
            True,
        ),
        (
            "report_agent_result",
            {"summary": "verified", "diagnostics": [{"code": "ok"}]},
            ReportAgentResultCommand,
            True,
        ),
    ),
)
def test_all_nine_subagent_commands_parse_to_closed_types(
    tool_name, arguments, expected_type, child
) -> None:
    command = build_subagent_tool_command(
        owner=_owner(tool_name, child=child),
        arguments=arguments,
    )
    assert isinstance(command, expected_type)
    assert command.command_kind == tool_name
    assert command.command_fingerprint.startswith("sha256:")


def test_child_payloads_are_deep_frozen_and_parent_cannot_report() -> None:
    progress = {"done": [1]}
    phase = build_subagent_tool_command(
        owner=_owner("report_agent_phase", child=True),
        arguments={"phase": "testing", "progress": progress},
    )
    assert isinstance(phase, ReportAgentPhaseCommand)
    progress["done"].append(2)
    assert thaw_json(phase.progress) == {"done": [1]}

    with pytest.raises(ValueError, match="bound subagent run"):
        build_subagent_tool_command(
            owner=_owner("report_agent_result"),
            arguments={"summary": "forged"},
        )


def test_command_factory_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="unexpected fields"):
        build_subagent_tool_command(
            owner=_owner("list_agents"),
            arguments={"mystery": True},
        )


class _MissingRuntime:
    async def wait_for_result(self, *_args, **_kwargs):
        raise SubagentNotFound("subagent:missing")


def test_runtime_port_maps_expected_not_found_to_typed_rejection() -> None:
    port = RuntimeSubagentControlPort(_MissingRuntime())  # type: ignore[arg-type]
    command = build_subagent_tool_command(
        owner=_owner("wait_agent"),
        arguments={"subagent_run_id": "subagent:missing"},
    )
    outcome = asyncio.run(port.execute(command))
    assert isinstance(outcome, SubagentToolRejectedOutcome)
    assert outcome.reject_code is SubagentToolRejectCode.NOT_FOUND
