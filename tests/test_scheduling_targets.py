"""Copyable schedule Targets, complete typed values and private revision authority."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from pulsara_agent.identity import derive_id
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.scheduling.contracts import ScheduledTaskError
from pulsara_agent.scheduling.requests import schedule_target, validate_action, validate_query
from pulsara_agent.scheduling.service import ScheduledTaskService

SESSION=derive_id("session","test","scheduled-handoff")
SOURCE=derive_id("schedule","test","source")
DESTINATION=derive_id("schedule","test","destination")
VALUES={"name":"Review","prompt":"Review the latest changes.","timezone":"Asia/Shanghai",
        "schedule":{"kind":"daily","start_date":"2030-01-01","time":"09:00"}}


def row(task_id=SOURCE,**changes):
    return {"id":task_id,"session_id":SESSION,**deepcopy(VALUES),"permission_mode":"read-only",
        "revision":7,"status":"PAUSED","next_run_at":None,"updated_at":datetime(2029,1,1,tzinfo=timezone.utc),**changes}


def service():
    value=ScheduledTaskService(NS(model_runtime=NS(freeze_resolution_snapshot=lambda: "frozen model resolution")))
    value._read=AsyncMock(return_value={SOURCE:7,DESTINATION:2})
    value._manage=AsyncMock()
    value.get=AsyncMock(side_effect=lambda identity: row(identity))
    return value


async def invoke(owner,arguments,mode=PermissionMode.BYPASS_PERMISSIONS):
    operation=owner.invoke_manage if "action" in arguments else owner.invoke_query
    return await operation(arguments,default_session_id=SESSION,permission_mode=mode)


def test_complete_query_targets_can_be_copied_into_management_without_public_revision():
    async def run():
        owner=service()
        source=row(prompt="完整提示词\r\n"+"中"*30000)
        owner._read=AsyncMock(return_value=[source])
        observed=await invoke(owner,{"status":"PAUSED"})
        record=observed.value.content[0].value
        assert record["target"] == schedule_target(SOURCE) and record["prompt"] == source["prompt"]
        assert set(record)=={"target","name","prompt","schedule","timezone","permission_mode","status","next_run_at"}
        assert observed.result_facts == {"schedule_revisions":{SOURCE:7}}
        owner._read.assert_awaited_once_with("list_model_scheduled_tasks",session_id=SESSION,status="PAUSED")
        owner._read=AsyncMock(return_value={SOURCE:7})
        owner._manage=AsyncMock(return_value=row(revision=8,prompt="updated"))
        updated=await invoke(owner,{"action":"update","target":record["target"],"values":{"prompt":"updated"}})
        owner._manage.assert_awaited_once_with("mutate_scheduled_task",session_id=SESSION,task_id=SOURCE,
            expected_revision=7,action="update",values={"prompt":"updated"})
        assert updated.value.public_fields == {"target":schedule_target(SOURCE)}
        assert updated.result_facts == {"schedule_revisions":{SOURCE:8}}
    asyncio.run(run())


def test_http_scope_stays_user_level_and_requires_an_explicit_creation_session():
    from pulsara_agent.desktop_app.scheduled_controller import ScheduledTaskController
    async def run():
        owner=NS(list=AsyncMock(return_value={"tasks":[],"next_cursor":None}),create=AsyncMock())
        controller=ScheduledTaskController(owner)
        await controller.list(NS(query={}))
        owner.list.assert_awaited_once_with(cursor=None,status=None,session_id=None)
        with pytest.raises(ScheduledTaskError):
            await controller.create(NS(json=AsyncMock(return_value={"session_id":None,"values":VALUES})))
        owner.create.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("kind",["existing","create"])
def test_switch_uses_both_saved_guards_and_keeps_permission_owned_by_the_saved_configuration(kind):
    async def run():
        owner=service()
        owner.get=AsyncMock(side_effect=lambda identity: row(identity,permission_mode="bypass-permissions" if identity==DESTINATION else "read-only"))
        owner._manage=AsyncMock(return_value=(row(revision=8),row(DESTINATION,revision=3,status="ACTIVE")))
        replacement=({"kind":"existing","target":schedule_target(DESTINATION)} if kind=="existing" else {"kind":"create","values":deepcopy(VALUES)})
        arguments={"action":"switch","target":schedule_target(SOURCE),"replacement":replacement}
        original=deepcopy(arguments)
        if kind=="existing":
            with pytest.raises(ScheduledTaskError) as error:
                await invoke(owner,arguments,PermissionMode.READ_ONLY)
            assert error.value.code=="PERMISSION_ESCALATION"
            owner._manage.assert_not_awaited()
        observed=await invoke(owner,arguments,PermissionMode.BYPASS_PERMISSIONS)
        called=owner._manage.call_args.kwargs
        assert called["expected_revision"]==7
        if kind=="existing":
            assert called["replacement"]=={**replacement,"expected_revision":2}
        else:
            assert called["replacement"]["values"]=={**VALUES,"permission_mode":"bypass-permissions"}
        assert observed.result_facts=={"schedule_revisions":{SOURCE:8,DESTINATION:3}}
        assert observed.value.public_fields["target"]==schedule_target(DESTINATION)
        assert arguments==original
    asyncio.run(run())


@pytest.mark.parametrize("arguments",[
    {"target":{**schedule_target(SOURCE),"revision":7}},
    {"target":schedule_target(SOURCE),"status":"ALL"},
    {"session":None},
    {"status":None},
    {"action":"pause","target":schedule_target(SOURCE),"expected_revision":7},
    {"action":"switch","target":schedule_target(SOURCE),"replacement":{"kind":"existing","target":schedule_target(SOURCE)}},
    {"action":"create","values":{**VALUES,"permission_mode":"bypass-permissions"}},
])
def test_closed_native_shapes_reject_wrong_authority_before_service_reads(arguments):
    async def run():
        owner=service()
        with pytest.raises(ScheduledTaskError):
            await invoke(owner,arguments)
        owner._read.assert_not_awaited()
        owner.get.assert_not_awaited()
        owner._manage.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("action",["update","pause","resume","delete"])
def test_management_requires_a_saved_same_session_observation(action):
    async def run():
        owner=service()
        owner._read=AsyncMock(return_value={})
        arguments={"action":action,"target":schedule_target(SOURCE)}
        if action=="update":
            arguments["values"]={"name":"new name"}
        validate_action(arguments)
        with pytest.raises(ScheduledTaskError) as error:
            await invoke(owner,arguments)
        assert error.value.code=="SNAPSHOT_REQUIRED"
        owner.get.assert_not_awaited()
        owner._manage.assert_not_awaited()
    asyncio.run(run())


def test_query_scope_filters_and_targets_are_distinct_closed_branches():
    assert validate_query({})=={}
    for status in ("ALL","ACTIVE","PAUSED"):
        assert validate_query({"status":status})=={"status":status}
    assert validate_query({"target":schedule_target(SOURCE)})=={"target":schedule_target(SOURCE)}


@pytest.mark.parametrize("source", ["TIMER","RUN_ONCE"])
def test_scheduled_input_keeps_exact_instruction_and_copyable_source_target(source):
    import json
    from pulsara_agent.llm.input import LLMTextPart
    from pulsara_agent.model_input.contracts import FrozenProviderInputItemKind
    from pulsara_agent.model_input.lowering import lower_canonical_item
    from pulsara_agent.scheduling.contracts import ScheduledProvenance, ScheduledTriggerSource
    provenance=ScheduledProvenance(SOURCE,7,"2030-01-01T01:00:00+00:00",ScheduledTriggerSource(source))
    original=(LLMTextPart("Exact saved instruction.\r\n原文"),)
    lowered=lower_canonical_item(NS(item_kind=FrozenProviderInputItemKind.USER,
        content=original,scheduled_input=provenance))
    header=lowered.message.content[0].text
    target,_=json.JSONDecoder().raw_decode(header[header.index('{'):])
    assert validate_query({"target":target})=={"target":schedule_target(SOURCE)}
    assert provenance.due_at_utc in header
    assert ("Automatic timer" in header)==(source=="TIMER")
    assert ("User selected Run once" in header)==(source=="RUN_ONCE")
    assert lowered.message.content[1:]==original
