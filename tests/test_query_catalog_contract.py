"""Merged tool branches stay exact locally, portable on wire, and owner scoped."""
from tests.support.model_config import scheduled_task_service
from dataclasses import replace

import asyncio
import pytest
from jsonschema import Draft202012Validator

from pulsara_agent.capability.builtin_catalog import (
    builtin_tool_catalog_entry,
    BuiltinToolAvailabilityKind, BuiltinTerminalPermissionRuleKind,
)
from pulsara_agent.capability.call_classifier import DefaultBuiltinToolCallClassifier
from pulsara_agent.capability.result_contracts import (
    generic_result_render_contract, terminal_process_result_render_contract,
    terminal_result_render_contract,
)
from pulsara_agent.conversation_kernel.tool_surface import (
    tool_observation_origin_for_binding,
)
from pulsara_agent.identity import derive_id
from pulsara_agent.llm.input import ToolSpec
from pulsara_agent.llm.adapters.openai.function_tools import (
    openai_chat_function_tool, openai_responses_function_tool,
)
from pulsara_agent.model_input.contracts import (
    ModelInputScopeKind,
)
from pulsara_agent.ports.tool_execution import ToolCall, ToolInvocationOwnerKind, thaw_tool_json_object
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.context import thaw_json
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from pulsara_agent.tool_permission import (
    AllowAllPermissionGate, PolicyPermissionGate, PermissionDecisionKind,
    preset_to_policy, TerminalAccess,
)


def target(kind, field, prefix):
    return {"kind": kind, field: derive_id(prefix, "query-catalog-test", kind)}


AGENT = target("AGENT", "agent_id", "session")
WORK = target("WORK", "work_id", "queue")
PROCESS = target("PROCESS", "process_id", "process")
CELL = target("CELL", "cell_id", "cell")
SCHEDULE = target("SCHEDULE", "task_id", "schedule")
MCP = {"kind": "MCP_TOOL", "server_id": "fixture", "remote_tool_name": "read"}
ENTRY = {"kind": "SESSION_ENTRY", "entry_id": derive_id("entry", "query-catalog-test", "saved-result")}
MEMORY = target("MEMORY", "memory_id", "memory")
SCHEDULE_VALUES = {
    "name": "readout", "prompt": "Summarize the current status.",
    "timezone": "Asia/Shanghai",
    "schedule": {"kind": "interval", "seconds": 900},
}
PATH = {"kind": "PATH", "path": "script.js"}
NEXT = {"kind": "NEXT", "token": "opaque"}
LEGAL = [
    ("query_capabilities", {"kind": "SKILL"}),
    ("query_capabilities", {"kind": "MCP", "query": "documents"}),
    ("query_capabilities", {"kind": "MCP", "target": MCP}),
    ("query_agents", {}), ("query_agents", {"target": AGENT}), ("query_agents", {"target": WORK}),
    ("exec_process", {"command": "true"}),
    ("query_processes", {}), ("query_processes", {"target": PROCESS}),
    ("manage_process", {"action": "wait", "target": PROCESS, "timeout_seconds": 2}),
    ("manage_process", {"action": "write", "target": PROCESS, "data": "x"}),
    ("manage_process", {"action": "close_stdin", "target": PROCESS}),
    ("manage_process", {"action": "kill", "target": PROCESS}),
    ("exec_cell", {"code": ""}), ("exec_cell", {"target": PATH}),
    ("query_cells", {}), ("query_cells", {"target": CELL}),
    ("manage_cell", {"action": "wait", "target": CELL}),
    ("manage_cell", {"action": "stop", "target": CELL}),
    ("query_schedules", {"status": "ALL"}), ("query_schedules", {"target": SCHEDULE}),
    ("manage_schedule", {"action": "create", "values": SCHEDULE_VALUES}),
    ("manage_schedule", {"action": "update", "target": SCHEDULE, "values":{"schedule": {"kind":"daily","start_date":"2026-11-16","time":"09:00"}}}),
    *[("manage_schedule", {"action": action, "target": SCHEDULE}) for action in ("pause", "resume", "delete")],
    ("manage_schedule", {"action": "switch", "target": SCHEDULE, "replacement": {"kind":"existing", "target": {"kind": "SCHEDULE", "task_id": derive_id("schedule", "query-catalog-test", "replacement")}}}),
    ("manage_schedule", {"action": "switch", "target": SCHEDULE, "replacement": {"kind":"create", "values": SCHEDULE_VALUES}}),
    ("read_file", {"target": PATH}), ("read_file", {"target": NEXT}),
    ("write_file", {"target": PATH, "tool_result_target": ENTRY}),
    ("read_session_content", {"target":{"kind":"SESSION","session_id":AGENT["agent_id"]}, "limit":-2}),
    ("read_session_content", {"target": ENTRY, "limit":2}),
    ("query_memory", {"target": MEMORY}),
    ("query_memory", {"query": "project preferences", "limit": 5}),
]


@pytest.mark.parametrize("name,arguments", LEGAL)
def test_canonical_calls_survive_both_actual_wire_serializers(name, arguments):
    descriptor = builtin_tool_catalog_entry(name).descriptor
    canonical = thaw_tool_json_object(descriptor.input_schema)
    Draft202012Validator(canonical).validate(arguments)
    spec = ToolSpec(name, descriptor.description, canonical)
    chat = openai_chat_function_tool(spec)["function"]
    responses = openai_responses_function_tool(spec)
    assert chat == {key: value for key, value in responses.items() if key != "type"}
    assert chat["strict"] is False
    assert chat["description"] == descriptor.description
    Draft202012Validator(chat["parameters"]).validate(arguments)


@pytest.mark.parametrize("name,arguments", [
    ("query_capabilities", {"target": MCP, "kind": "MCP_TOOL"}),
    ("query_capabilities", {"target": MCP, "parent": {"kind": "MCP_SERVER", "runtime_server_id": "fixture"}}),
    ("query_agents", {"target": {"kind": "AGENT", "agent_id": "ROOT"}}),
    ("query_agents", {"target": WORK, "max_items": 1}),
    ("query_agents", {"target": WORK, "cursor": "x"}),
    ("query_processes", {"target": PROCESS, "include_running": True}),
    ("query_cells", {"target": CELL, "cursor": CELL["cell_id"]}),
    ("query_cells", {"offset_chars": 0}),
    ("query_cells", {"max_items": True}),
    ("query_cells", {"target": CELL, "max_chars": "2"}),
    ("query_schedules", {"target": SCHEDULE, "session": None}),
    ("query_schedules", {"status": None}),
    ("read_session_content", {"target":{"kind":"SESSION","session_id":AGENT["agent_id"]},"limit":1}),
    ("read_session_content", {"target":ENTRY,"limit":0}),
    ("exec_cell", {}),
    ("exec_cell", {"code": "return 1", "path": "script.js"}),
    ("manage_cell", {"action": "stop", "target": CELL, "yield_time_ms": 1}),
    ("manage_cell", {"action": "wait", "cell_id": CELL["cell_id"]}),
    ("manage_process", {"action": "poll", "target": PROCESS, "timeout_seconds": 2}),
    ("manage_process", {"action": "list"}),
    ("manage_schedule", {"action": "list"}),
] + [(name, {"target": None}) for name in (
    "query_capabilities", "query_agents", "query_processes", "query_cells", "query_schedules",
)])
def test_merged_branches_reject_cross_branch_fields(name, arguments):
    schema = thaw_tool_json_object(builtin_tool_catalog_entry(name).descriptor.input_schema)
    assert not Draft202012Validator(schema).is_valid(arguments)


@pytest.mark.parametrize("name,arguments,read_only", [
    ("query_capabilities", {"kind":"SKILL"}, True), ("query_agents", {}, True),
    ("query_processes", {}, True),
    ("query_cells", {}, True), ("query_schedules", {}, True),

    ("manage_process", {"action": "wait"}, True),
    ("manage_process", {"action": "write"}, False),
    ("manage_process", {"action": "kill"}, False),
    ("manage_cell", {"action": "wait"}, True),
    ("manage_cell", {"action": "stop"}, False),
    ("manage_schedule", {"action": "pause"}, False),
    ("read_file", {"target": NEXT}, True), ("write_file", {"target": {"kind": "PATH", "path": "saved.txt"}, "tool_result_target": ENTRY}, False),
])
def test_permission_classifier_follows_selected_action(name, arguments, read_only):
    classification = DefaultBuiltinToolCallClassifier().classify_builtin(ToolCall("test", name, arguments))
    assert classification.effective_read_only is read_only


@pytest.mark.parametrize("name,arguments,allowed", [
    ("query_capabilities", {"kind":"SKILL"}, True),
    ("query_schedules", {}, True),
    ("manage_schedule", {"action": "delete", "target": SCHEDULE}, False),
    ("manage_process", {"action": "wait", "target": PROCESS}, True),
    ("manage_process", {"action": "write", "target": PROCESS, "data": "x"}, False),
    ("write_file", {"target": {"kind": "PATH", "path": "saved.txt"}, "tool_result_target": ENTRY}, False),
    ("query_agents", {}, False),
])
def test_read_only_permission_gate_uses_new_catalog(name, arguments, allowed, tmp_path):
    gate = PolicyPermissionGate(preset_to_policy(PermissionMode.READ_ONLY), AllowAllPermissionGate(), workspace_root=tmp_path)
    decision = asyncio.run(gate.evaluate([ToolCall("test", name, arguments)]))
    assert (decision.kind is PermissionDecisionKind.ALLOW) is allowed


@pytest.mark.parametrize("mode", list(PermissionMode))
def test_shared_query_retains_bypass_permission_requirement(mode, tmp_path):
    gate = PolicyPermissionGate(preset_to_policy(mode), AllowAllPermissionGate(), workspace_root=tmp_path)
    decision = asyncio.run(gate.evaluate([ToolCall("query", "query_agents", {})]))
    assert (decision.kind is PermissionDecisionKind.ALLOW) is (mode is PermissionMode.BYPASS_PERMISSIONS)


def test_terminal_queries_keep_terminal_access_requirement(tmp_path):
    policy = replace(preset_to_policy(PermissionMode.BYPASS_PERMISSIONS), terminal=TerminalAccess.OFF)
    gate = PolicyPermissionGate(policy, AllowAllPermissionGate(), workspace_root=tmp_path)
    for name in ("query_processes",):
        entry = builtin_tool_catalog_entry(name)
        assert entry.permission_contract.terminal_rule.kind is BuiltinTerminalPermissionRuleKind.ALL_ACTIONS
        decision = asyncio.run(gate.evaluate([ToolCall("test", name, {})]))
        assert decision.kind is PermissionDecisionKind.DENY


def test_actual_js_directory_keeps_shared_calls_and_excludes_native_owners(tmp_path):
    from tests.test_code_mode_entry_admission import _port
    from tests.support.round3 import prepare_test_direct_tool_surface

    async def scenario():
        port = _port(tmp_path)
        try:
            prepared = prepare_test_direct_tool_surface(port, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            directory = port.freeze_code_mode_tool_directory(prepared,
                effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            names = {spec.name for spec in directory.tool_specs}
            assert {"query_capabilities", "query_agents", "exec_process", "query_processes", "manage_process", "read_file", "write_file", "decide", "run_agent", "wait_agent"} <= names
            assert not names & {"wait_work", "exec_cell", "query_cells", "manage_cell", "query_monitors", "manage_monitor", "query_schedules", "manage_schedule", "search_memory", "get_memory"}
            for name in (names & {spec.name for spec in prepared.model_surface.tool_specs}) - {"read_file"}:
                assert directory.binding(name) == prepared.binding(name)
            script_read = next(spec for spec in directory.tool_specs if spec.name == "read_file")
            assert Draft202012Validator(thaw_json(script_read.parameters)).is_valid({"target":PATH})
            assert not Draft202012Validator(thaw_json(script_read.parameters)).is_valid({"target":NEXT})
            for name in ("exec_process", "query_processes", "manage_process"):
                assert tool_observation_origin_for_binding(prepared.binding(name)) is ToolObservationOrigin.TERMINAL_PROCESS
        finally:
            await port.aclose()
    asyncio.run(scenario())
    for name in ("exec_cell", "query_cells", "manage_cell"):
        entry = builtin_tool_catalog_entry(name)
        assert entry.availability_requirement.allowed_invocation_owners == (ToolInvocationOwnerKind.HOST_MAIN_RUN,)
    for name in ("exec_cell", "query_cells", "manage_cell"):
        assert builtin_tool_catalog_entry(name).availability_requirement.kind is BuiltinToolAvailabilityKind.REQUIRES_CODE_MODE
    query = builtin_tool_catalog_entry("query_agents")
    assert query.availability_requirement.kind is BuiltinToolAvailabilityKind.REQUIRES_SHARED_AGENT_CONTROL
    assert set(query.availability_requirement.allowed_invocation_owners) == {
        ToolInvocationOwnerKind.HOST_MAIN_RUN, ToolInvocationOwnerKind.SUBAGENT,
    }


@pytest.mark.parametrize("name", ["list_capabilities", "inspect_capability", "list_agents", "inspect_agent", "terminal", "terminal_process", "terminal_monitor", "manage_monitor", "query_monitors", "scheduled_tasks", "exec", "wait_exec", "stop_exec", "artifact_read", "artifact_export", "memory_get", "memory_search"])
def test_old_public_names_have_no_catalog_or_classifier_path(name):
    with pytest.raises(KeyError):
        builtin_tool_catalog_entry(name)
    with pytest.raises(KeyError):
        DefaultBuiltinToolCallClassifier().classify_builtin(ToolCall("test", name, {}))




def test_terminal_result_contracts_match_executor_shapes():
    assert builtin_tool_catalog_entry("exec_process").descriptor.result_render_contract is terminal_result_render_contract()
    assert builtin_tool_catalog_entry("manage_process").descriptor.result_render_contract is terminal_process_result_render_contract()
    assert builtin_tool_catalog_entry("query_processes").descriptor.result_render_contract is generic_result_render_contract()


def test_native_subagent_query_binding_and_authorization_keep_management_closed(tmp_path):
    from types import SimpleNamespace
    from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
    from pulsara_agent.conversation_kernel.tool_contracts import KernelToolAuthorizationKind
    from pulsara_agent.conversation_kernel.memory.contracts import FrozenModelCallMemoryContext
    from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import AGENT_TOOLS, validate_collaboration_parameters
    from pulsara_agent.primitives.run_permission import build_run_permission_snapshot, RunPermissionAdmissionSource
    from pulsara_agent.identity import random_id
    from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
    from pulsara_agent.conversation_kernel.tool_policy import DefaultToolDispatchAuthorizationPolicy
    from tests.support.round3 import prepare_test_direct_tool_surface

    def validate(*, tool_name, arguments):
        try:
            validate_collaboration_parameters(tool_name, arguments)
        except (TypeError, ValueError, KeyError) as error:
            return str(error)
        return None

    async def scenario():
        port = DirectKernelToolPort(scheduled_tasks=scheduled_task_service(), workspace_root=tmp_path, host_owner_id="query-host",
            session_id=random_id("session"), live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy())
        port.bind_subagent_port(SimpleNamespace(tool_names=AGENT_TOOLS,
            validate_arguments=validate, bind_cell_observer=lambda _: None))
        try:
            surface = prepare_test_direct_tool_surface(port,
                conversation_scope_kind=ModelInputScopeKind.SUBAGENT,
                effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            names = {item.name for item in surface.model_surface.tool_specs}
            assert {"query_agents", "send_agent_message", "wait_message"} <= names
            assert not {"run_agent", "stop_agent", "exec_cell", "query_cells", "manage_cell"} & names
            prefix = tuple(item.canonical_bytes for item in surface.model_surface.tool_specs)
            borrow = port.borrow_tool_surface(surface)
            try:
                for mode in PermissionMode:
                    permission = build_run_permission_snapshot(snapshot_id=random_id("permission"),
                        requested_mode=mode, effective_mode=mode,
                        admission_source=RunPermissionAdmissionSource.USER_SUBMISSION)
                    authorization = await port.authorize(tool_name="query_agents", arguments={},
                        tool_call_id=random_id("call"), turn_id=random_id("turn"),
                        assistant_entry_id=random_id("entry"), permission_snapshot=permission,
                        surface_borrow=borrow, memory_context=FrozenModelCallMemoryContext())
                    assert authorization.kind is (
                        KernelToolAuthorizationKind.ALLOW if mode is PermissionMode.BYPASS_PERMISSIONS
                        else KernelToolAuthorizationKind.PERMISSION_DENIED)
                assert tuple(item.canonical_bytes for item in surface.model_surface.tool_specs) == prefix
            finally:
                borrow.close()
        finally:
            await port.aclose()
    asyncio.run(scenario())


def test_code_mode_read_file_contract_is_path_only():
    from pulsara_agent.capability.builtin_catalog import execution_catalog_entry
    native=thaw_tool_json_object(builtin_tool_catalog_entry("read_file").descriptor.input_schema)
    script=thaw_tool_json_object(execution_catalog_entry("read_file",code_mode=True).descriptor.input_schema)
    assert Draft202012Validator(native).is_valid({"target":NEXT})
    assert Draft202012Validator(script).is_valid({"target":PATH})
    assert not Draft202012Validator(script).is_valid({"target":NEXT})
