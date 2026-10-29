"""Merged tool branches stay exact locally, portable on wire, and owner scoped."""
from dataclasses import replace

import asyncio
import pytest
from jsonschema import Draft202012Validator

from pulsara_agent.capability.builtin_catalog import (
    builtin_tool_catalog, builtin_tool_catalog_entry,
    BuiltinToolAvailabilityKind, BuiltinTerminalPermissionRuleKind,
)
from pulsara_agent.capability.call_classifier import DefaultBuiltinToolCallClassifier
from pulsara_agent.capability.result_contracts import (
    generic_result_render_contract, terminal_process_result_render_contract,
    terminal_result_render_contract,
)
from pulsara_agent.conversation_kernel.tool_surface import (
    BuiltinExecutionPolicyRef, CodeModeToolDirectory, PreparedKernelToolSurface,
    PreparedToolExecutionBinding, ProcessLocalToolSurfaceAccess,
    tool_observation_origin_for_binding,
)
from pulsara_agent.identity import derive_id
from pulsara_agent.llm.input import ToolSpec
from pulsara_agent.llm.adapters.openai.function_tools import (
    openai_chat_function_tool, openai_responses_function_tool,
)
from pulsara_agent.model_input.contracts import (
    FrozenModelToolSurface, FrozenToolSpec, ModelInputScopeKind, model_tool_surface_fingerprint,
)
from pulsara_agent.ports.tool_execution import ToolCall, ToolInvocationOwnerKind, thaw_tool_json_object
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from pulsara_agent.primitives.tool_result_projection import (
    classify_tool_result_delivery, ToolResultDeliveryRequirement, ToolResultFullDeliveryReason,
)
from pulsara_agent.tool_permission import (
    AllowAllPermissionGate, PolicyPermissionGate, PermissionDecisionKind,
    preset_to_policy, TerminalAccess,
)


def target(kind, field, prefix):
    return {"kind": kind, field: derive_id(prefix, "query-catalog-test", kind)}


AGENT = target("AGENT", "agent_id", "session")
WORK = target("WORK", "work_id", "queue")
PROCESS = target("PROCESS", "process_id", "process")
MONITOR = target("MONITOR", "monitor_id", "monitor")
CELL = target("CELL", "cell_id", "cell")
SCHEDULE = target("SCHEDULE", "task_id", "schedule")
MCP = {"kind": "MCP_TOOL", "server_id": "fixture", "tool_name": "read"}
ARTIFACT = target("ARTIFACT", "artifact_id", "artifact")
MEMORY = target("MEMORY", "memory_id", "memory")
SCHEDULE_VALUES = {
    "name": "readout", "prompt": "Summarize the current status.",
    "timezone": "Asia/Shanghai", "permission_mode": "read-only",
    "schedule": {"contract": "scheduled-rule:v1", "kind": "interval",
                 "anchor_at_utc": "2026-10-30T00:00:00Z", "seconds": 60},
}


LEGAL = [
    ("query_capabilities", {}),
    ("query_capabilities", {"kind": "MCP_TOOL", "offset": 0, "limit": 2}),
    ("query_capabilities", {"target": MCP}),
    ("query_agents", {}),
    ("query_agents", {"target": AGENT, "max_items": 2}),
    ("query_agents", {"target": WORK}),
    ("exec_process", {"command": "true"}),
    ("query_processes", {}),
    ("query_processes", {"include_finished": False}),
    ("query_processes", {"target": PROCESS}),
    ("manage_process", {"action": "poll", "target": PROCESS, "since_cursor": None}),
    ("manage_process", {"action": "wait", "target": PROCESS, "timeout_seconds": 2}),
    ("manage_process", {"action": "write", "target": PROCESS, "data": "x"}),
    ("manage_process", {"action": "submit", "target": PROCESS, "data": "x"}),
    ("manage_process", {"action": "close_stdin", "target": PROCESS}),
    ("manage_process", {"action": "kill", "target": PROCESS}),
    ("query_monitors", {}),
    ("query_monitors", {"target": MONITOR}),
    ("manage_monitor", {"action": "register", "target": PROCESS}),
    ("manage_monitor", {"action": "cancel", "target": MONITOR}),
    ("exec_cell", {"code": ""}),
    ("exec_cell", {"path": "script.js"}),
    ("query_cells", {"max_items": 10000}),
    ("query_cells", {"cursor": CELL["cell_id"]}),
    ("query_cells", {"target": CELL, "offset_chars": 0, "max_chars": 2}),
    ("manage_cell", {"action": "wait", "target": CELL}),
    ("manage_cell", {"action": "stop", "target": CELL}),
    ("query_schedules", {"session": None, "status": "PAUSED"}),
    ("query_schedules", {"target": SCHEDULE}),
    ("manage_schedule", {"action": "create", "values": SCHEDULE_VALUES, "status": None}),
    ("manage_schedule", {"action": "update", "target": SCHEDULE, "expected_revision": 1, "values": SCHEDULE_VALUES}),
    *[("manage_schedule", {"action": action, "target": SCHEDULE, "expected_revision": 1}) for action in ("pause", "resume", "delete")],
    ("manage_schedule", {"action": "switch", "target": SCHEDULE, "expected_revision": 1,
                         "replacement": {"kind": "existing", "target": {"kind": "SCHEDULE", "task_id": derive_id("schedule", "query-catalog-test", "replacement")}, "expected_revision": 1}}),
    ("manage_schedule", {"action": "switch", "target": SCHEDULE, "expected_revision": 1,
                         "replacement": {"kind": "create", "values": SCHEDULE_VALUES}}),
    ("read_artifact", {"target": ARTIFACT}),
    ("export_artifact", {"target": ARTIFACT, "path": "saved-output.txt"}),
    ("get_memory", {"target": MEMORY}),
    ("search_memory", {"query": "project preferences"}),
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
    ("query_agents", {"target": WORK, "max_items": 1}),
    ("query_agents", {"target": WORK, "cursor": "x"}),
    ("query_processes", {"target": PROCESS, "include_running": True}),
    ("query_monitors", {"cursor": "x"}),
    ("query_monitors", {"target": PROCESS}),
    ("query_cells", {"target": CELL, "cursor": CELL["cell_id"]}),
    ("query_cells", {"offset_chars": 0}),
    ("query_cells", {"max_items": True}),
    ("query_cells", {"target": CELL, "max_chars": "2"}),
    ("query_schedules", {"target": SCHEDULE, "session": None}),
    ("query_schedules", {"status": None}),
    ("exec_cell", {}),
    ("exec_cell", {"code": "return 1", "path": "script.js"}),
    ("manage_cell", {"action": "stop", "target": CELL, "yield_time_ms": 1}),
    ("manage_cell", {"action": "wait", "cell_id": CELL["cell_id"]}),
    ("manage_process", {"action": "poll", "target": PROCESS, "timeout_seconds": 2}),
    ("manage_process", {"action": "list"}),
    ("manage_monitor", {"action": "list"}),
    ("manage_schedule", {"action": "list"}),
] + [(name, {"target": None}) for name in (
    "query_capabilities", "query_agents", "query_processes", "query_monitors", "query_cells", "query_schedules",
)])
def test_merged_branches_reject_cross_branch_fields(name, arguments):
    schema = thaw_tool_json_object(builtin_tool_catalog_entry(name).descriptor.input_schema)
    assert not Draft202012Validator(schema).is_valid(arguments)


@pytest.mark.parametrize("name,arguments,read_only", [
    ("query_capabilities", {}, True), ("query_agents", {}, True),
    ("query_processes", {}, True), ("query_monitors", {}, True),
    ("query_cells", {}, True), ("query_schedules", {}, True),
    ("manage_process", {"action": "poll"}, True),
    ("manage_process", {"action": "wait"}, True),
    ("manage_process", {"action": "write"}, False),
    ("manage_process", {"action": "kill"}, False),
    ("manage_monitor", {"action": "register"}, False),
    ("manage_monitor", {"action": "cancel"}, False),
    ("manage_cell", {"action": "wait"}, True),
    ("manage_cell", {"action": "stop"}, False),
    ("manage_schedule", {"action": "pause"}, False),
    ("read_artifact", {}, True), ("export_artifact", {}, False),
])
def test_permission_classifier_follows_selected_action(name, arguments, read_only):
    classification = DefaultBuiltinToolCallClassifier().classify_builtin(ToolCall("test", name, arguments))
    assert classification.effective_read_only is read_only


@pytest.mark.parametrize("name,arguments,allowed", [
    ("query_capabilities", {}, True),
    ("query_schedules", {}, True),
    ("manage_schedule", {"action": "delete", "target": SCHEDULE}, False),
    ("manage_process", {"action": "poll", "target": PROCESS}, True),
    ("manage_process", {"action": "write", "target": PROCESS, "data": "x"}, False),
    ("export_artifact", {}, False),
    ("query_agents", {}, False),
])
def test_read_only_permission_gate_uses_new_catalog(name, arguments, allowed, tmp_path):
    gate = PolicyPermissionGate(preset_to_policy(PermissionMode.READ_ONLY), AllowAllPermissionGate(), workspace_root=tmp_path)
    decision = asyncio.run(gate.evaluate([ToolCall("test", name, arguments)]))
    assert (decision.kind is PermissionDecisionKind.ALLOW) is allowed


def test_terminal_queries_keep_terminal_access_requirement(tmp_path):
    policy = replace(preset_to_policy(PermissionMode.BYPASS_PERMISSIONS), terminal=TerminalAccess.OFF)
    gate = PolicyPermissionGate(policy, AllowAllPermissionGate(), workspace_root=tmp_path)
    for name in ("query_processes", "query_monitors"):
        entry = builtin_tool_catalog_entry(name)
        assert entry.permission_contract.terminal_rule.kind is BuiltinTerminalPermissionRuleKind.ALL_ACTIONS
        decision = asyncio.run(gate.evaluate([ToolCall("test", name, {})]))
        assert decision.kind is PermissionDecisionKind.DENY


def _prepared_catalog():
    entries = builtin_tool_catalog()
    specs = tuple(FrozenToolSpec(entry.name, entry.descriptor.description,
        freeze_json(thaw_tool_json_object(entry.descriptor.input_schema)), entry.descriptor.fingerprint()) for entry in entries)
    surface = FrozenModelToolSurface(ModelInputScopeKind.ROOT, specs,
        model_tool_surface_fingerprint(ModelInputScopeKind.ROOT, specs))
    bindings = tuple(PreparedToolExecutionBinding(entry.name, entry.descriptor.fingerprint(), "test-executor",
        BuiltinExecutionPolicyRef(entry.name, entry.entry_fingerprint)) for entry in entries)
    access = ProcessLocalToolSurfaceAccess(1, 1, ModelInputScopeKind.ROOT, object())
    return PreparedKernelToolSurface(surface, bindings, access)


def test_actual_js_directory_keeps_shared_calls_and_excludes_native_owners():
    prepared = _prepared_catalog()
    directory = CodeModeToolDirectory.freeze(prepared)
    names = {spec.name for spec in directory.tool_specs}
    assert {"query_capabilities", "query_agents", "exec_process", "query_processes", "manage_process", "read_artifact", "export_artifact"} <= names
    assert not names & {"exec_cell", "query_cells", "manage_cell", "query_monitors", "manage_monitor", "query_schedules", "manage_schedule", "search_memory", "get_memory"}
    for name in names:
        assert directory.binding(name) is prepared.binding(name)
    for name in ("exec_cell", "query_cells", "manage_cell", "query_agents"):
        entry = builtin_tool_catalog_entry(name)
        assert entry.availability_requirement.allowed_invocation_owners == (ToolInvocationOwnerKind.HOST_MAIN_RUN,)
    for name in ("exec_cell", "query_cells", "manage_cell"):
        assert builtin_tool_catalog_entry(name).availability_requirement.kind is BuiltinToolAvailabilityKind.REQUIRES_CODE_MODE
    for name in ("exec_process", "query_processes", "manage_process", "query_monitors", "manage_monitor"):
        assert tool_observation_origin_for_binding(prepared.binding(name)) is ToolObservationOrigin.TERMINAL_PROCESS


@pytest.mark.parametrize("name", ["list_capabilities", "inspect_capability", "list_agents", "inspect_agent", "terminal", "terminal_process", "terminal_monitor", "scheduled_tasks", "exec", "wait_exec", "stop_exec", "artifact_read", "artifact_export", "memory_get", "memory_search"])
def test_old_public_names_have_no_catalog_or_classifier_path(name):
    with pytest.raises(KeyError):
        builtin_tool_catalog_entry(name)
    with pytest.raises(KeyError):
        DefaultBuiltinToolCallClassifier().classify_builtin(ToolCall("test", name, {}))


@pytest.mark.parametrize("name,arguments,reason", [
    ("exec_cell", {}, ToolResultFullDeliveryReason.CODE_MODE_OUTPUT_PAGE),
    ("manage_cell", {"action": "wait"}, ToolResultFullDeliveryReason.CODE_MODE_OUTPUT_PAGE),
    ("manage_cell", {"action": "stop"}, ToolResultFullDeliveryReason.CODE_MODE_OUTPUT_PAGE),
    ("query_cells", {}, ToolResultFullDeliveryReason.CELL_QUERY_PAGE),
    ("query_cells", {"target": CELL}, ToolResultFullDeliveryReason.CELL_QUERY_PAGE),
    ("query_capabilities", {}, ToolResultFullDeliveryReason.MCP_DIRECTORY_PAGE),
    ("query_capabilities", {"target": MCP}, ToolResultFullDeliveryReason.MCP_INSPECT_SCHEMA),
    ("query_capabilities", {"target": {"kind": "SKILL", "skill_path": "/fixture/SKILL.md"}}, None),
    ("read_artifact", {}, ToolResultFullDeliveryReason.ARTIFACT_PAGE),
    ("export_artifact", {}, ToolResultFullDeliveryReason.ARTIFACT_EXPORT_LOCATION),
])
def test_full_delivery_depends_on_new_query_and_management_branch(name, arguments, reason):
    delivery = classify_tool_result_delivery(tool_name=name, result_state="SUCCESS", public_arguments=arguments)
    assert delivery.reason is reason
    assert delivery.requirement is (ToolResultDeliveryRequirement.FULL_REQUIRED if reason else ToolResultDeliveryRequirement.BEST_AVAILABLE)
    assert classify_tool_result_delivery(tool_name=name, result_state="ERROR", public_arguments=arguments).requirement is ToolResultDeliveryRequirement.BEST_AVAILABLE


def test_terminal_result_contracts_match_executor_shapes():
    assert builtin_tool_catalog_entry("exec_process").descriptor.result_render_contract is terminal_result_render_contract()
    assert builtin_tool_catalog_entry("manage_process").descriptor.result_render_contract is terminal_process_result_render_contract()
    assert builtin_tool_catalog_entry("query_processes").descriptor.result_render_contract is generic_result_render_contract()
