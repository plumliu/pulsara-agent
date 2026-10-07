"""Public shape, exact choices and tool-only defaults at the builtin boundary."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jsonschema import Draft202012Validator

from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry
from pulsara_agent.conversation_kernel.subagent import _parse_context
from pulsara_agent.llm.adapters.openai.function_tools import (
    lower_openai_function_parameters,
)
from pulsara_agent.llm.model_catalog import (
    ReasoningEffortChoices,
    ReasoningSelectableControls,
)
from pulsara_agent.llm.model_connections import reasoning_selection_from_dict
from pulsara_agent.llm.model_target import (
    ModelReasoningSelectionInvalid,
    default_reasoning_selection,
    validate_reasoning_selection,
)
from pulsara_agent.ports.terminal import (
    parse_terminal_monitor_input,
    parse_terminal_process_input,
)
from pulsara_agent.ports.tool_execution import thaw_tool_json_object
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.scheduling.contracts import ScheduledTaskError
from pulsara_agent.scheduling.requests import validate_action
from pulsara_agent.scheduling.service import ScheduledTaskService


def schema(name):
    return thaw_tool_json_object(
        builtin_tool_catalog_entry(name).descriptor.input_schema
    )


def assert_shape(name, arguments, valid):
    canonical = schema(name)
    assert Draft202012Validator(canonical).is_valid(arguments) is valid
    if valid:
        # A supported local call must remain reachable through both wire APIs'
        # shared projection. The converse is deliberately not required.
        assert Draft202012Validator(
            lower_openai_function_parameters(canonical)
        ).is_valid(arguments)


@pytest.mark.parametrize("name", ["spawn_agent", "create_agent_tasks"])
@pytest.mark.parametrize(
    "context,valid",
    [
        ({"mode": "none"}, True),
        ({"mode": "last_n", "turns": 1}, True),
        ({"mode": "last_n", "turns": 3}, True),
        ({"mode": "worker_history", "task_id": "task:old"}, True),
        ({}, False),
        (None, False),
        ({"mode": "last_n"}, False),
        ({"mode": "last_n", "turns": True}, False),
        ({"mode": "last_n", "turns": 4}, False),
        ({"mode": "last_n", "turns": 1, "task_id": "task:old"}, False),
        ({"mode": "none", "turns": 1}, False),
        ({"mode": "none", "task_id": None}, False),
        ({"mode": "worker_history"}, False),
        ({"mode": "worker_history", "task_id": ""}, False),
    ],
)
def test_subagent_context_public_variants(name, context, valid):
    task = {"task": "Inspect the fixture", "context": context}
    assert_shape(name, task if name == "spawn_agent" else {"tasks": [task]}, valid)
    if valid:
        _parse_context(context)


@pytest.mark.parametrize("name", ["spawn_agent", "create_agent_tasks"])
@pytest.mark.parametrize(
    "reasoning,valid",
    [
        ({"kind": "effort", "value": None}, True),
        ({"kind": "effort", "value": "high"}, True),
        ({"kind": "toggle", "enabled": False}, True),
        ({"kind": "budget_tokens", "tokens": 1024}, True),
        (None, False),
        ({}, False),
        ({"kind": "effort"}, False),
        ({"kind": "effort", "value": None, "enabled": False}, False),
        ({"kind": "toggle", "enabled": None}, False),
        ({"kind": "budget_tokens", "tokens": True}, False),
    ],
)
def test_subagent_reasoning_exact_shape(name, reasoning, valid):
    task = {
        "task": "Inspect the fixture",
        "model": {"connection_id": "connection", "reasoning": reasoning},
    }
    assert_shape(name, task if name == "spawn_agent" else {"tasks": [task]}, valid)
    if valid:
        assert reasoning_selection_from_dict(reasoning) is not None


def test_null_effort_is_an_exact_choice_not_an_omitted_selection():
    choice = reasoning_selection_from_dict({"kind": "effort", "value": None})
    with_null = ReasoningSelectableControls(
        effort=ReasoningEffortChoices((None, "high"))
    )
    without_null = ReasoningSelectableControls(effort=ReasoningEffortChoices(("high",)))
    validate_reasoning_selection(with_null, choice)
    with pytest.raises(ModelReasoningSelectionInvalid):
        validate_reasoning_selection(without_null, choice)
    with pytest.raises(ModelReasoningSelectionInvalid):
        validate_reasoning_selection(with_null, None)
    assert default_reasoning_selection(with_null) != choice


@pytest.mark.parametrize(
    "name", ["search_sessions", "search_session_content", "read_session_content"]
)
def test_session_first_page_and_cursor_only_shapes(name):
    first = {"query": "literal"} if name == "search_session_content" else {}
    assert_shape(name, first, True)
    assert_shape(name, {"cursor": "returned", "limit": 1}, True)
    assert_shape(name, {"cursor": None}, False)
    assert_shape(name, {"cursor": "returned", "limit": None}, False)
    filters = (
        {"query": "same", "lifecycle": "ALL"}
        if name == "search_sessions"
        else {
            "session_id": "same-session",
            "include_tools": False,
            **(
                {"query": "same"}
                if name == "search_session_content"
                else {"entry_id": "same-entry", "direction": "newer"}
            ),
        }
    )
    for key, value in filters.items():
        assert_shape(name, {"cursor": "returned", key: value}, False)
        assert_shape(name, {"cursor": "returned", key: None}, False)
    assert_shape(
        name, {"cursor": "returned", "max_chars": 512}, name == "read_session_content"
    )


@pytest.mark.parametrize("query", [None, "", " \t\n", "\u3000"])
def test_first_content_search_needs_actual_keywords(query):
    assert_shape("search_session_content", {"query": query}, False)
    assert_shape("search_session_content", {}, False)


@pytest.mark.parametrize(
    "args,valid",
    [
        ({}, True),
        ({"timeout_seconds": 0}, True),
        ({"task_ids": ["task:one"]}, True),
        ({"task_ids": ["task:one"], "settle": "first"}, True),
        ({"settle": "all"}, False),
        ({"settle": None}, False),
        ({"task_ids": []}, False),
        ({"task_ids": None}, False),
    ],
)
def test_wait_join_shape(args, valid):
    assert_shape("wait_agent", args, valid)


def scheduled_args(action):
    if action == "list":
        return {"action": action, "status": "PAUSED", "cursor": "scheduled:previous"}
    if action == "create":
        return {
            "action": action,
            "values": {
                "name": "check",
                "prompt": "Report once",
                "timezone": "Asia/Shanghai",
                "permission_mode": "read-only",
                "schedule": {
                    "contract": "scheduled-rule:v1",
                    "kind": "interval",
                    "anchor_at_utc": "2026-10-07T00:00:00Z",
                    "seconds": 60,
                },
            },
        }
    return {
        "action": action,
        "task_id": "scheduled:one",
        "expected_revision": 1,
        "client_command_id": "command:original",
        "request_at_utc": "2026-10-07T00:00:00Z",
    }


@pytest.mark.parametrize("action", ["list", "create", "run_now"])
@pytest.mark.parametrize(
    "session_field,expected",
    [
        ({}, "current"),
        ({"session_id": None}, "current"),
        ({"session_id": "other"}, "other"),
    ],
)
def test_scheduled_tool_defaults_before_dispatch_without_losing_filters_or_identity(
    action, session_field, expected
):
    async def run():
        service = ScheduledTaskService(SimpleNamespace())
        target = AsyncMock(return_value={"ok": True})
        setattr(service, action, target)
        arguments = {**scheduled_args(action), **session_field}
        original = deepcopy(arguments)
        assert_shape("scheduled_tasks", arguments, True)
        await service.invoke(
            arguments,
            default_session_id="current",
            permission_mode=PermissionMode.BYPASS_PERMISSIONS,
        )
        dispatched = {key: value for key, value in original.items() if key != "action"}
        dispatched["session_id"] = expected
        if action == "run_now":
            target.assert_awaited_once_with(dispatched)
        else:
            target.assert_awaited_once_with(**dispatched)
        assert arguments == original

    asyncio.run(run())


def test_scheduled_service_none_keeps_its_unfiltered_meaning():
    async def run():
        service = ScheduledTaskService(SimpleNamespace())
        service._read = AsyncMock(return_value=([], None))
        await service.list()
        service._read.assert_awaited_once_with(
            "list_scheduled_tasks", cursor=None, status=None, session_id=None
        )
        service._read.reset_mock()
        await service.invoke(
            {"action": "list", "session_id": None},
            default_session_id="current",
            permission_mode=PermissionMode.READ_ONLY,
        )
        service._read.assert_awaited_once_with(
            "list_scheduled_tasks", cursor=None, status=None, session_id="current"
        )

    asyncio.run(run())
    with pytest.raises(ScheduledTaskError):
        validate_action({"action": "list", "session_id": None})


@pytest.mark.parametrize("action", ["get", "update", "pause", "resume", "delete"])
def test_scheduled_null_does_not_erase_forbidden_fields(action):
    async def run():
        service = ScheduledTaskService(SimpleNamespace())
        service.get = AsyncMock()
        service.mutate = AsyncMock()
        args = {"action": action, "task_id": "scheduled:one", "session_id": None}
        if action != "get":
            args["expected_revision"] = 1
        if action == "update":
            args["values"] = scheduled_args("create")["values"]
        assert_shape("scheduled_tasks", args, False)
        with pytest.raises(ScheduledTaskError):
            await service.invoke(
                args,
                default_session_id="current",
                permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            )
        service.get.assert_not_awaited()
        service.mutate.assert_not_awaited()

    asyncio.run(run())


def test_scheduled_wire_keeps_action_signatures_and_pagination_guidance():
    canonical = schema("scheduled_tasks")
    wire = lower_openai_function_parameters(canonical)
    assert set(wire["properties"]["action"]["enum"]) == {
        branch["properties"]["action"]["const"] for branch in canonical["oneOf"]
    }
    for branch in canonical["oneOf"]:
        action = branch["properties"]["action"]["const"]
        text = next(
            part
            for part in wire["description"].split(". ")
            if part.startswith(action + ": ")
        )
        required, optional = (
            text.removeprefix(action + ": required ")
            .removesuffix(".")
            .split("; optional ")
        )
        required_set = set() if required == "none" else set(required.split(", "))
        optional_set = set() if optional == "none" else set(optional.split(", "))
        assert required_set == set(branch["required"]) - {"action"}
        assert required_set | optional_set == set(branch["properties"]) - {"action"}
    assert (
        "repeat the same status/session_id filters"
        in wire["properties"]["cursor"]["description"]
    )


@pytest.mark.parametrize(
    "name,parse,args",
    [
        (
            "terminal_process",
            parse_terminal_process_input,
            {"action": "poll", "process_id": "p", "timeout_seconds": 1},
        ),
        (
            "terminal_monitor",
            parse_terminal_monitor_input,
            {"action": "list", "conditions": {}},
        ),
    ],
)
def test_terminal_flattening_preserves_allowed_fields_guidance(name, parse, args):
    canonical = schema(name)
    wire = lower_openai_function_parameters(canonical)
    for branch in canonical["oneOf"]:
        properties = branch["properties"]
        action = properties["action"]["const"]
        signatures = wire["description"].split("): ", 1)[1]
        signature = next(
            part for part in signatures.split(". ") if part.startswith(action + ": ")
        )
        actual = signature.split(": ", 1)[1].rstrip(".")
        allowed = set() if actual == "none" else set(actual.split(", "))
        assert allowed == set(properties) - {"action"}
    assert_shape(name, args, False)
    with pytest.raises(ValueError):
        parse(args)


def test_existing_empty_values_remain_operations():
    assert_shape("todo", {"items": []}, True)
    assert_shape("write_file", {"path": "empty.txt", "content": ""}, True)
    assert_shape("report_agent_result", {"summary": "done", "data": {}}, True)
    assert_shape("create_agent_tasks", {"tasks": []}, False)
    base = {"action": "register", "process_id": "p"}
    assert (
        parse_terminal_monitor_input(
            {**base, "conditions": {"output": None}}
        ).conditions.output
        is None
    )
    assert (
        parse_terminal_monitor_input(
            {**base, "conditions": {"output": {}}}
        ).conditions.output
        is not None
    )
