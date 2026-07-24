from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from pulsara_agent.capability.builtin_provider import builtin_tool_descriptors
from pulsara_agent.event import EventContext
from pulsara_agent.capability.result_contracts import (
    result_render_contract_for_tool,
    terminal_monitor_result_render_contract,
    terminal_process_result_render_contract,
)
from pulsara_agent.capability.result_semantics import unbounded_error_preview
from pulsara_agent.llm.adapters.openai.chat_completions import _tool_to_chat_tool
from pulsara_agent.llm.adapters.openai.responses import _tool_to_responses_tool
from pulsara_agent.llm.input import ToolSpec
from pulsara_agent.message import ToolResultState
from pulsara_agent.primitives.long_horizon import LongHorizonActionClass
from pulsara_agent.primitives.tool_result import (
    TerminalMonitorCancellationEssentialFact,
    TerminalMonitorErrorEssentialFact,
    TerminalMonitorInventoryEssentialFact,
    TerminalMonitorRegistrationEssentialFact,
    ToolResultRenderVariantCode,
)
from pulsara_agent.runtime.context_input.render import _essential_envelope
from pulsara_agent.runtime.tool_action import (
    builtin_tool_action_policy,
    default_tool_action_classifier_registry,
)
from pulsara_agent.runtime.terminal.notification import (
    TerminalNotificationCapacityError,
)
from pulsara_agent.runtime.permission import (
    AllowAllPermissionGate,
    ApprovalPolicy,
    EffectivePermissionPolicy,
    PermissionDecisionKind,
    PermissionProfile,
    PolicyPermissionGate,
    TerminalAccess,
)
from pulsara_agent.runtime.tool_taxonomy import TERMINAL_TOOL_NAMES
from pulsara_agent.terminal_public_api import (
    TERMINAL_MONITOR_TOOL_DESCRIPTION,
    TERMINAL_PROCESS_TOOL_DESCRIPTION,
    TERMINAL_TOOL_DESCRIPTION,
    TerminalMonitorRegisterInput,
    builtin_tool_input_contract_binding,
    parse_terminal_monitor_input,
    parse_terminal_process_input,
    resolve_terminal_monitor_public_policy,
)
from pulsara_agent.tools.base import ToolCall, ToolRuntimeContext
from pulsara_agent.tools.builtins.terminal import TerminalTool
from pulsara_agent.tools.builtins.terminal_monitor import TerminalMonitorTool
from pulsara_agent.tools.builtins.terminal_process import TerminalProcessTool


def test_terminal_input_binding_is_shared_by_descriptor_and_tool(tmp_path) -> None:
    descriptors = {item.name: item for item in builtin_tool_descriptors()}
    tools = {
        "terminal_process": TerminalProcessTool(tmp_path),
        "terminal_monitor": TerminalMonitorTool(tmp_path),
    }

    for name, tool in tools.items():
        binding = builtin_tool_input_contract_binding(name)  # type: ignore[arg-type]
        descriptor_schema = descriptors[name].input_schema
        assert descriptor_schema is not None
        assert descriptor_schema == tool.parameters == binding.input_schema
        assert len(descriptor_schema["oneOf"]) == (
            8 if name == "terminal_process" else 3
        )


def test_terminal_descriptions_have_one_public_contract_owner(tmp_path) -> None:
    descriptors = {item.name: item for item in builtin_tool_descriptors()}
    tools = {
        "terminal": TerminalTool(tmp_path),
        "terminal_process": TerminalProcessTool(tmp_path),
        "terminal_monitor": TerminalMonitorTool(tmp_path),
    }
    expected = {
        "terminal": TERMINAL_TOOL_DESCRIPTION,
        "terminal_process": TERMINAL_PROCESS_TOOL_DESCRIPTION,
        "terminal_monitor": TERMINAL_MONITOR_TOOL_DESCRIPTION,
    }

    for name, tool in tools.items():
        assert descriptors[name].description == tool.description == expected[name]

    assert all(
        status in TERMINAL_TOOL_DESCRIPTION
        for status in ("running", "success", "error", "timeout", "blocked", "killed")
    )
    assert "<copy exact process_id>" in TERMINAL_PROCESS_TOOL_DESCRIPTION
    assert "do not loop wait" in TERMINAL_PROCESS_TOOL_DESCRIPTION
    assert "omit conditions to disable progress and heartbeat" in (
        TERMINAL_MONITOR_TOOL_DESCRIPTION
    )
    assert "bounded expiry" in TERMINAL_MONITOR_TOOL_DESCRIPTION
    assert "user or task actually requires" in TERMINAL_MONITOR_TOOL_DESCRIPTION


def test_terminal_public_schemas_are_strict_branch_specific() -> None:
    process_schema = builtin_tool_input_contract_binding(
        "terminal_process"
    ).input_schema
    monitor_schema = builtin_tool_input_contract_binding(
        "terminal_monitor"
    ).input_schema

    assert process_schema["type"] == "object"
    assert monitor_schema["type"] == "object"
    assert _actions(process_schema) == {
        "list",
        "log",
        "poll",
        "wait",
        "write",
        "submit",
        "close_stdin",
        "kill",
    }
    assert _actions(monitor_schema) == {"register", "list", "cancel"}
    assert all(
        branch["additionalProperties"] is False for branch in process_schema["oneOf"]
    )
    assert all(
        branch["additionalProperties"] is False for branch in monitor_schema["oneOf"]
    )
    register = _branch(monitor_schema, "register")
    conditions = register["properties"]["conditions"]
    delivery = register["properties"]["delivery"]
    lifetime = register["properties"]["lifetime"]
    assert conditions["additionalProperties"] is False
    assert delivery["additionalProperties"] is False
    assert lifetime["additionalProperties"] is False
    heartbeat = conditions["properties"]["heartbeat_interval_seconds"]["anyOf"][0]
    assert (heartbeat["minimum"], heartbeat["maximum"]) == (5, 1_800)
    output_chars = delivery["properties"]["max_output_chars"]
    assert (
        output_chars["minimum"],
        output_chars["maximum"],
        output_chars["default"],
    ) == (
        512,
        32_000,
        4_000,
    )
    assert conditions["default"] == {
        "output": None,
        "heartbeat_interval_seconds": None,
    }
    assert delivery["default"] == {
        "max_output_chars": 4_000,
        "minimum_progress_observation_interval_seconds": 5,
    }
    assert lifetime["default"] == {"maximum_duration_seconds": 36_000}
    assert '"title"' not in json.dumps(process_schema)
    assert '"title"' not in json.dumps(monitor_schema)
    assert _missing_property_descriptions(process_schema) == []
    assert _missing_property_descriptions(monitor_schema) == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "list", "process_id": "process:x"},
        {"action": "wait", "process_id": "process:x", "timeout_seconds": 0},
        {"action": "poll", "process_id": "process:x", "max_output_chars": 511},
        {"action": "wait", "process_id": "process:x", "timeout_seconds": True},
        {"action": "monitor", "process_id": "process:x"},
        {"action": "list_monitors"},
        {"action": "cancel_monitor", "monitor_id": "monitor:x"},
    ],
)
def test_terminal_process_rejects_cross_branch_and_removed_actions(arguments) -> None:
    with pytest.raises(ValidationError):
        parse_terminal_process_input(arguments)


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "monitor", "process_id": "process:x"},
        {"action": "list_monitors"},
        {"action": "cancel_monitor", "monitor_id": "monitor:x"},
    ],
)
def test_terminal_process_removed_actions_return_typed_malformed_result(
    tmp_path,
    arguments,
) -> None:
    result = TerminalProcessTool(tmp_path).execute(
        ToolCall(id="call:legacy", name="terminal_process", arguments=arguments)
    )

    payload = json.loads(result.output)
    assert result.status is ToolResultState.ERROR
    assert payload["status"] == "malformed_arguments"
    assert payload["policy_code"] == "terminal_process_malformed_arguments"
    assert result.prepared_terminal_monitor_registration is None
    assert result.prepared_terminal_monitor_cancellation is None


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "list", "process_id": "process:x"},
        {"action": "cancel", "monitor_id": "monitor:x", "conditions": {}},
        {
            "action": "register",
            "process_id": "process:x",
            "conditions": {"heartbeat_interval_seconds": 4},
        },
        {
            "action": "register",
            "process_id": "process:x",
            "delivery": {"max_output_chars": True},
        },
        {
            "action": "register",
            "process_id": "process:x",
            "lifetime": {"kind": "bounded"},
        },
    ],
)
def test_terminal_monitor_rejects_cross_branch_and_invalid_bounds(arguments) -> None:
    with pytest.raises(ValidationError):
        parse_terminal_monitor_input(arguments)


def test_terminal_monitor_public_factory_freezes_final_progress_policy() -> None:
    parsed = parse_terminal_monitor_input(
        {"action": "register", "process_id": "process:x"}
    )
    assert isinstance(parsed, TerminalMonitorRegisterInput)

    resolved = resolve_terminal_monitor_public_policy(parsed)

    assert resolved.delivery.max_output_chars == 4_000
    assert resolved.delivery.maximum_pending_progress_observations == 1
    assert resolved.delivery.maximum_committed_progress_observations == 119
    assert resolved.delivery.progress_observation_rate_window_seconds == 600
    assert resolved.delivery.maximum_progress_observations_per_rate_window == 60
    assert resolved.lifetime.kind == "process_lifetime"
    assert resolved.lifetime.maximum_duration_seconds == 36_000


def test_terminal_tool_schema_is_preserved_by_provider_serializers() -> None:
    schema = builtin_tool_input_contract_binding("terminal_monitor").schema_copy()
    spec = ToolSpec(
        name="terminal_monitor",
        description="monitor",
        parameters=schema,
    )

    chat = _tool_to_chat_tool(spec)
    responses = _tool_to_responses_tool(spec)
    deepseek_compatible_chat = _tool_to_chat_tool(spec)

    assert chat["function"]["parameters"] == schema
    assert deepseek_compatible_chat["function"]["parameters"] == schema
    assert responses["parameters"] == schema


def test_terminal_monitor_has_dedicated_result_contract() -> None:
    monitor = terminal_monitor_result_render_contract()
    process = terminal_process_result_render_contract()
    monitor_codes = {item.variant_code for item in monitor.allowed_variants}
    process_codes = {item.variant_code for item in process.allowed_variants}

    assert result_render_contract_for_tool("terminal_monitor") == monitor
    assert monitor.semantics_builder_id == "tool-result-semantics:terminal-monitor"
    assert monitor_codes == {
        ToolResultRenderVariantCode.TERMINAL_MONITOR_REGISTRATION,
        ToolResultRenderVariantCode.TERMINAL_MONITOR_INVENTORY,
        ToolResultRenderVariantCode.TERMINAL_MONITOR_CANCELLATION,
        ToolResultRenderVariantCode.TERMINAL_MONITOR_ERROR,
        ToolResultRenderVariantCode.TERMINAL_MONITOR_ADAPTER_ERROR,
    }
    assert monitor_codes.isdisjoint(process_codes)


@pytest.mark.parametrize(
    ("essential", "expected_action"),
    [
        (
            TerminalMonitorRegistrationEssentialFact(
                capture_policy_fingerprint="capture:test",
                process_id="process:x",
                monitor_id="monitor:x",
                expires_at_utc="2026-07-22T00:00:00Z",
                status="running",
                exit_code=None,
                output_truncated=False,
                terminal_session_id="default",
                backend_type="local",
            ),
            "register",
        ),
        (
            TerminalMonitorInventoryEssentialFact(
                capture_policy_fingerprint="capture:test",
                status="success",
                monitor_summaries=(),
                omitted_monitor_count=0,
                summaries_truncated=False,
            ),
            "list",
        ),
        (
            TerminalMonitorCancellationEssentialFact(
                capture_policy_fingerprint="capture:test",
                monitor_id="monitor:x",
                outcome="cancelled",
            ),
            "cancel",
        ),
        (
            TerminalMonitorErrorEssentialFact(
                capture_policy_fingerprint="capture:test",
                requested_action="register",
                process_id="process:x",
                monitor_id=None,
                status="blocked",
                error=unbounded_error_preview("denied"),
                policy_code="denied",
            ),
            "register",
        ),
    ],
)
def test_terminal_monitor_essential_envelope_keeps_monitor_owner(
    essential,
    expected_action,
) -> None:
    payload = _essential_envelope(
        SimpleNamespace(
            essential=essential,
            artifacts=(),
            terminal_payload_timing=None,
        ),
        observation={},
    )

    assert "terminal_process_action" not in payload
    assert payload["terminal_monitor_action"] == expected_action
    if essential.kind == "terminal_monitor_error":
        assert payload["requested_action"] == "register"


def test_terminal_monitor_permission_taxonomy_and_long_horizon_matrix() -> None:
    assert "terminal_monitor" in TERMINAL_TOOL_NAMES
    policy = builtin_tool_action_policy("terminal_monitor")
    registry = default_tool_action_classifier_registry()
    expected = {
        "register": LongHorizonActionClass.PROCESS_CONTROL,
        "list": LongHorizonActionClass.EVIDENCE_HYDRATION,
        "cancel": LongHorizonActionClass.PROCESS_CONTROL,
    }
    for action, action_class in expected.items():
        classified = registry.classify(
            call=ToolCall(
                id=f"call:{action}",
                name="terminal_monitor",
                arguments={"action": action},
            ),
            descriptor_id="builtin:terminal_monitor",
            descriptor_fingerprint="sha256:" + "0" * 64,
            policy=policy,
        )
        assert classified.action_class is action_class


def test_terminal_monitor_list_is_observation_but_register_and_cancel_schedule() -> (
    None
):
    gate = PolicyPermissionGate(
        EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.NEVER,
            terminal=TerminalAccess.ASK,
        ),
        AllowAllPermissionGate(),
    )

    decisions = {
        action: asyncio.run(
            gate.evaluate(
                [
                    ToolCall(
                        id=f"call:{action}",
                        name="terminal_monitor",
                        arguments={"action": action},
                    )
                ]
            )
        ).kind
        for action in ("register", "list", "cancel")
    }

    assert decisions == {
        "register": PermissionDecisionKind.WAIT_FOR_USER,
        "list": PermissionDecisionKind.ALLOW,
        "cancel": PermissionDecisionKind.WAIT_FOR_USER,
    }


@pytest.mark.parametrize(
    "reason_code",
    [
        "terminal_notification_capacity_exhausted",
        "terminal_monitor_already_active_for_process",
    ],
)
def test_terminal_monitor_expected_registration_rejection_is_typed(
    tmp_path,
    reason_code,
) -> None:
    class RejectingCoordinator:
        def prepare_registration(self, **_kwargs):
            raise TerminalNotificationCapacityError(
                "expected monitor request rejection",
                reason_code=reason_code,
            )

    tool = TerminalMonitorTool(
        tmp_path,
        owner_host_session_id="host:test",
        terminal_monitor_coordinator=RejectingCoordinator(),
    )
    result = tool.execute(
        ToolCall(
            id="call:monitor-rejected",
            name="terminal_monitor",
            arguments={"action": "register", "process_id": "process:test"},
        ),
        runtime_context=ToolRuntimeContext(
            runtime_session_id="runtime:test",
            event_context=EventContext(
                run_id="run:test",
                turn_id="turn:test",
                reply_id="reply:test",
            ),
            run_entry_kind="host_main_run",
        ),
    )

    payload = json.loads(result.output)
    assert result.status is ToolResultState.ERROR
    assert payload["status"] == "blocked"
    assert payload["policy_code"] == reason_code
    assert result.semantics_input is not None
    assert (
        result.semantics_input.semantics_input_kind
        is ToolResultRenderVariantCode.TERMINAL_MONITOR_ERROR
    )


def test_production_source_contains_no_removed_terminal_process_actions() -> None:
    source_root = Path(__file__).parents[1] / "src" / "pulsara_agent"
    forbidden = {"monitor", "list_monitors", "cancel_monitor"}
    findings: list[str] = []
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in forbidden:
                findings.append(f"{path.relative_to(source_root)}:{node.lineno}")
    assert findings == []


def _actions(schema: dict[str, object]) -> set[str]:
    return {
        str(branch["properties"]["action"]["const"])
        for branch in schema["oneOf"]  # type: ignore[index,union-attr]
    }


def _branch(schema: dict[str, object], action: str) -> dict[str, object]:
    return next(
        branch
        for branch in schema["oneOf"]  # type: ignore[index,union-attr]
        if branch["properties"]["action"]["const"] == action
    )


def _missing_property_descriptions(
    schema: object,
    *,
    path: str = "$",
) -> list[str]:
    if isinstance(schema, list):
        return [
            finding
            for index, item in enumerate(schema)
            for finding in _missing_property_descriptions(
                item,
                path=f"{path}[{index}]",
            )
        ]
    if not isinstance(schema, dict):
        return []
    findings: list[str] = []
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, value in properties.items():
            if not isinstance(value, dict) or not value.get("description"):
                findings.append(f"{path}.properties.{name}")
    for key, value in schema.items():
        findings.extend(_missing_property_descriptions(value, path=f"{path}.{key}"))
    return findings
