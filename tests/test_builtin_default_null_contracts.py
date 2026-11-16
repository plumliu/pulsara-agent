"""Omitted defaults, explicit nulls and exact provider choices remain distinct."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jsonschema import Draft202012Validator

from pulsara_agent.capability.builtin_catalog import execution_catalog_entry
from pulsara_agent.identity import random_id
from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import prepared_request, validate_collaboration_parameters
from pulsara_agent.llm.adapters.openai.function_tools import lower_openai_function_parameters
from pulsara_agent.llm.model_catalog import ReasoningEffortChoices, ReasoningSelectableControls
from pulsara_agent.llm.model_connections import reasoning_selection_from_dict
from pulsara_agent.llm.model_target import ModelReasoningSelectionInvalid, default_reasoning_selection, validate_reasoning_selection
from pulsara_agent.ports.tool_execution import thaw_tool_json_object
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.scheduling.contracts import ScheduledTaskError
from pulsara_agent.scheduling.requests import validate_action
from pulsara_agent.scheduling.service import ScheduledTaskService


def schema(name):
    return thaw_tool_json_object(execution_catalog_entry(name).descriptor.input_schema)


def assert_shape(name, arguments, valid):
    canonical = schema(name)
    assert Draft202012Validator(canonical).is_valid(arguments) is valid
    if valid:
        assert Draft202012Validator(lower_openai_function_parameters(canonical)).is_valid(arguments)


def dispatch_arguments(parameters):
    return {"members":[{"agent_id":random_id("session"),"work_id":random_id("queue"),"parameters":parameters}]}


@pytest.mark.parametrize("field", ["materials", "depends_on", "target", "work_title"])
def test_run_agent_omitted_options_do_not_accept_explicit_null(field):
    omitted = prepared_request({"instruction":"Inspect"})
    assert omitted.create_agent and omitted.context_turns == () and omitted.materials == ()
    assert omitted.depends_on == () and omitted.work_title is None
    arguments = dispatch_arguments({"instruction":"Inspect"})
    assert_shape("run_agent", arguments, True)
    validate_collaboration_parameters("run_agent", arguments)
    arguments["members"][0]["parameters"][field] = None
    assert_shape("run_agent", arguments, False)
    with pytest.raises(ValueError):
        validate_collaboration_parameters("run_agent", arguments)


def test_run_agent_materials_select_actual_context_and_work_references():
    work = random_id("queue")
    parameters = {"instruction":"Inspect", "materials":[{"kind":"CONTEXT_MATERIAL","turns":2},{"kind":"WORK","work_id":work}], "depends_on":[]}
    assert_shape("run_agent", dispatch_arguments(parameters), True)
    request = prepared_request(parameters)
    assert request.context_turns == (2,) and request.materials == (work,) and request.depends_on == ()


def test_null_effort_is_an_exact_choice_not_an_omitted_selection():
    choice = reasoning_selection_from_dict({"kind":"effort","value":None})
    with_null = ReasoningSelectableControls(effort=ReasoningEffortChoices((None,"high")))
    without_null = ReasoningSelectableControls(effort=ReasoningEffortChoices(("high",)))
    validate_reasoning_selection(with_null, choice)
    with pytest.raises(ModelReasoningSelectionInvalid):
        validate_reasoning_selection(without_null, choice)
    with pytest.raises(ModelReasoningSelectionInvalid):
        validate_reasoning_selection(with_null, None)
    assert default_reasoning_selection(with_null) != choice


SESSION = random_id("session")
TASK = random_id("schedule")
VALUES = {"name":"check","prompt":"Report once","schedule":{"kind":"interval","seconds":900}}


@pytest.mark.parametrize("status_fields", [{}, {"status":"PAUSED"}, {"status":"ACTIVE"}])
def test_schedule_create_defaults_only_omitted_fields_and_freezes_actual_turn_permission(status_fields):
    async def run():
        service = ScheduledTaskService(SimpleNamespace())
        service.create = AsyncMock(return_value={"id":TASK,"session_id":SESSION,"revision":1,
            "status":status_fields.get("status","PAUSED"),"next_run_at":None})
        arguments = {"action":"create","values":deepcopy(VALUES),**status_fields}
        original = deepcopy(arguments)
        assert_shape("manage_schedule", arguments, True)
        observation = await service.invoke_manage(arguments, default_session_id=SESSION,
            permission_mode=PermissionMode.READ_ONLY)
        service.create.assert_awaited_once_with(session_id=SESSION,
            values={**VALUES,"permission_mode":"read-only"},status=status_fields.get("status"))
        assert observation.result_facts == {"schedule_revisions":{TASK:1}}
        assert arguments == original
    asyncio.run(run())


@pytest.mark.parametrize("status", [None,"","active","COMPLETED",False,0,[],{}])
def test_schedule_native_create_rejects_explicit_null_and_invalid_status(status):
    arguments={"action":"create","values":VALUES,"status":status}
    assert_shape("manage_schedule", arguments, False)
    with pytest.raises(ScheduledTaskError):
        validate_action(arguments)


def test_schedule_user_list_is_user_scoped_and_native_list_defaults_to_current_session():
    async def run():
        service=ScheduledTaskService(SimpleNamespace())
        service._read=AsyncMock(return_value=([],None))
        await service.list()
        service._read.assert_awaited_once_with("list_scheduled_tasks",cursor=None,status=None,session_id=None)
        service._read=AsyncMock(return_value=[])
        await service.invoke_query({},default_session_id=SESSION,permission_mode=PermissionMode.READ_ONLY)
        service._read.assert_awaited_once_with("list_model_scheduled_tasks",session_id=SESSION,status=None)
    asyncio.run(run())


@pytest.mark.parametrize("arguments", [{"status":None},{"target":None},{"session":None},{"cursor":None}])
def test_schedule_null_is_not_a_scope_override(arguments):
    assert_shape("query_schedules",arguments,False)
    async def run():
        service=ScheduledTaskService(SimpleNamespace())
        service._read=AsyncMock()
        with pytest.raises(ScheduledTaskError):
            await service.invoke_query(arguments,default_session_id=SESSION,permission_mode=PermissionMode.READ_ONLY)
        service._read.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("field", ["timezone","schedule","name","prompt"])
def test_schedule_values_null_is_not_an_omitted_update(field):
    arguments={"action":"update","target":{"kind":"SCHEDULE","task_id":TASK},"values":{field:None}}
    assert_shape("manage_schedule",arguments,False)
    with pytest.raises(ScheduledTaskError):
        validate_action(arguments)


def test_existing_empty_values_retain_their_current_operations():
    assert_shape("todo",{"items":[]},True)
    assert_shape("run_agent",dispatch_arguments({"instruction":"Inspect","materials":[],"depends_on":[]}),True)
    assert_shape("wait_agent",{"agents":[]},False)
    assert_shape("wait_agent",{"works":[]},False)
    assert_shape("wait_message",{},True)
    assert_shape("wait_message",{"timeout_seconds":None},False)
