"""Public shape, exact choices and tool-only defaults at the builtin boundary."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jsonschema import Draft202012Validator

from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry
from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import (
    prepared_request,
    validate_collaboration_parameters,
)
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


@pytest.mark.parametrize("field", ["context", "profile", "target", "name"])
def test_run_agent_omitted_creation_options_and_explicit_null_have_distinct_meaning(
    field,
):
    omitted = prepared_request({"instruction": "Inspect"})
    assert (
        omitted.create_agent
        and omitted.context_mode == "none"
        and omitted.profile is None
    )
    with pytest.raises(ValueError):
        validate_collaboration_parameters(
            "run_agent", {"instruction": "Inspect", field: None}
        )


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


def scheduled_args(action):
    if action == "list":
        return {
            "action": action,
            "status": "PAUSED",
            "cursor": "schedule:bcud6jffakbgubat6jskaqqhme",
        }
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
    raise AssertionError(f"unsupported scoped action: {action}")


@pytest.mark.parametrize("action", ["list", "create"])
@pytest.mark.parametrize(
    "session_field,expected",
    [
        ({}, "session:6g3pqrtwdp2ilmnlzfmxxxhelu"),
        ({"session": None}, "session:6g3pqrtwdp2ilmnlzfmxxxhelu"),
        (
            {
                "session": {
                    "kind": "SESSION",
                    "session_id": "session:pusyod5zvaz4i6yode652m7ble",
                }
            },
            "session:pusyod5zvaz4i6yode652m7ble",
        ),
    ],
)
def test_scheduled_tool_defaults_before_dispatch_without_losing_filters_or_identity(
    action, session_field, expected
):
    async def run():
        service = ScheduledTaskService(SimpleNamespace())
        target = AsyncMock(
            return_value=(
                {"tasks": [], "next_cursor": None}
                if action == "list"
                else {"id": "schedule:fixture", "session_id": expected}
            )
        )
        setattr(service, action, target)
        arguments = {**scheduled_args(action), **session_field}
        original = deepcopy(arguments)
        await service.invoke(
            arguments,
            default_session_id="session:6g3pqrtwdp2ilmnlzfmxxxhelu",
            permission_mode=PermissionMode.BYPASS_PERMISSIONS,
        )
        dispatched = {
            key: value
            for key, value in original.items()
            if key not in {"action", "session"}
        }
        dispatched["session_id"] = expected
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
            {"action": "list", "session": None},
            default_session_id="session:6g3pqrtwdp2ilmnlzfmxxxhelu",
            permission_mode=PermissionMode.READ_ONLY,
        )
        service._read.assert_awaited_once_with(
            "list_scheduled_tasks",
            cursor=None,
            status=None,
            session_id="session:6g3pqrtwdp2ilmnlzfmxxxhelu",
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
        args = {
            "action": action,
            "target": {"kind": "SCHEDULE", "task_id": "scheduled:one"},
            "session": None,
        }
        if action != "get":
            args["expected_revision"] = 1
        if action == "update":
            args["values"] = scheduled_args("create")["values"]
        with pytest.raises(ScheduledTaskError):
            await service.invoke(
                args,
                default_session_id="session:6g3pqrtwdp2ilmnlzfmxxxhelu",
                permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            )
        service.get.assert_not_awaited()
        service.mutate.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize(
    "status_fields", [{}, {"status": None}, {"status": "PAUSED"}, {"status": "ACTIVE"}]
)
def test_scheduled_create_status_schema_and_dispatch(status_fields):
    async def run():
        service = ScheduledTaskService(SimpleNamespace())
        service.create = AsyncMock(
            return_value={
                "id": "schedule:fixture",
                "session_id": "session:6g3pqrtwdp2ilmnlzfmxxxhelu",
            }
        )
        arguments = {**scheduled_args("create"), **status_fields}
        await service.invoke(
            arguments,
            default_session_id="session:6g3pqrtwdp2ilmnlzfmxxxhelu",
            permission_mode=PermissionMode.BYPASS_PERMISSIONS,
        )
        service.create.assert_awaited_once_with(
            session_id="session:6g3pqrtwdp2ilmnlzfmxxxhelu",
            values=arguments["values"],
            **status_fields,
        )

    asyncio.run(run())


@pytest.mark.parametrize("status", ["", "active", "COMPLETED", False, 0, [], {}])
def test_scheduled_create_rejects_invalid_status(status):
    from pulsara_agent.scheduling.requests import creation_status

    arguments = {**scheduled_args("create"), "status": status}
    with pytest.raises(ScheduledTaskError):
        validate_action(arguments)
    with pytest.raises(ScheduledTaskError):
        creation_status(status)


def test_existing_empty_values_remain_operations():
    assert_shape("todo", {"items": []}, True)
    assert_shape(
        "run_agent", {"instruction": "Inspect", "materials": [], "depends_on": []}, True
    )
    assert_shape("wait_agent", {"targets": []}, False)
    base = {"action": "register", "target": {"kind": "PROCESS", "process_id": "p"}}
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


def test_scheduled_tool_rejects_run_now_and_switch_checks_permission():
    from pulsara_agent.scheduling.service import ScheduledTaskService
    from pulsara_agent.scheduling.contracts import ScheduledTaskError
    from pulsara_agent.primitives.permission import PermissionMode

    async def scenario():
        service = ScheduledTaskService(SimpleNamespace())
        service.run_now = AsyncMock()
        with pytest.raises(ScheduledTaskError) as error:
            await service.invoke(
                {
                    "action": "run_now",
                    "task_id": "schedule:" + "a" * 26,
                    "expected_revision": 1,
                    "client_command_id": "command:knzz7jzjjydzbbeubc3tt7q32m",
                    "request_at_utc": "2026-10-13T00:00:00Z",
                },
                default_session_id="session:" + "a" * 26,
                permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            )
        assert error.value.code == "INVALID_ACTION"
        assert_shape(
            "scheduled_tasks",
            {
                "action": "run_now",
                "task_id": "schedule:" + "a" * 26,
                "expected_revision": 1,
                "client_command_id": "command:knzz7jzjjydzbbeubc3tt7q32m",
                "request_at_utc": "2026-10-13T00:00:00Z",
            },
            False,
        )
        service.run_now.assert_not_awaited()
        service.get = AsyncMock(return_value={"permission_mode": "bypass-permissions"})
        service.switch = AsyncMock()
        with pytest.raises(ScheduledTaskError) as error:
            await service.invoke(
                {
                    "action": "switch",
                    "target": {"kind": "SCHEDULE", "task_id": "schedule:" + "a" * 26},
                    "expected_revision": 1,
                    "replacement": {
                        "kind": "existing",
                        "target": {
                            "kind": "SCHEDULE",
                            "task_id": "schedule:" + "b" * 25 + "a",
                        },
                        "expected_revision": 2,
                    },
                },
                default_session_id="session:" + "a" * 26,
                permission_mode=PermissionMode.READ_ONLY,
            )
        assert error.value.code == "PERMISSION_ESCALATION"
        service.switch.assert_not_awaited()

    asyncio.run(scenario())
