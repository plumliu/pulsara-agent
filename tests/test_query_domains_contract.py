"""Query branches share native schemas and preserve their existing domain owners."""

import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from jsonschema import Draft202012Validator

from pulsara_agent.capability.source_query import (
    CapabilityQueryError,
    parse_query,
    query_input_schema,
)
from pulsara_agent.conversation_kernel.capability_query import CapabilitySourceQuery
from pulsara_agent.conversation_kernel.subagents.collaboration_api import CollaborationToolPort
from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import (
    agent_target,
    query_agents_input_schema,
    validate_collaboration_parameters,
    work_target,
)
from pulsara_agent.identity import derive_id
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.scheduling.contracts import ScheduledTaskError
from pulsara_agent.scheduling.requests import (
    creation_status,
    manage_schedule_schema,
    query_schedules_schema,
    schedule_target,
    validate_action,
    validate_query,
)
from pulsara_agent.scheduling.service import ScheduledTaskService

ROOT = derive_id("session", "query-domains", "root")
AGENT = derive_id("session", "query-domains", "agent")
OTHER = derive_id("session", "query-domains", "other")
WORK = derive_id("queue", "query-domains", "work")
SCHEDULE = derive_id("schedule", "query-domains", "schedule")
VALUES = {
    "name": "Review",
    "prompt": "Review the current changes.",
    "timezone": "Asia/Shanghai",
    "permission_mode": "read-only",
    "schedule": {
        "contract": "scheduled-rule:v1",
        "kind": "daily",
        "start_date": "2030-01-01",
        "time": "09:00",
    },
}


@pytest.mark.parametrize("schema,parse,valid,invalid", [
    (
        query_input_schema(), parse_query,
        [{}, {"limit": 1}, {"parent": {"kind": "MCP_SERVER", "runtime_server_id": "s"}},
         {"target": {"kind": "MCP_TOOL", "server_id": "s", "tool_name": "t"}}],
        [{"target": None}, {"action": "list"},
         {"target": {"kind": "MCP_SERVER", "runtime_server_id": "s"}, "limit": 1},
         {"target": {"kind": "MCP_SERVER", "runtime_server_id": "s"}, "parent": {"kind": "PLUGIN", "scope": "USER", "plugin_id": "p"}}],
    ),
    (
        query_agents_input_schema(), lambda a: validate_collaboration_parameters("query_agents", a),
        [{}, {"max_items": 50}, {"target": agent_target(AGENT), "max_items": 1}, {"target": work_target(WORK)}],
        [{"target": None}, {"action": "list"}, {"target": work_target(WORK), "cursor": WORK},
         {"target": work_target(WORK), "max_items": 1}, {"max_items": True}, {"max_items": 51}],
    ),
    (
        query_schedules_schema(), validate_query,
        [{}, {"session": None}, {"status": "ACTIVE", "cursor": SCHEDULE}, {"target": schedule_target(SCHEDULE)}],
        [{"target": None}, {"action": "list"}, {"status": None},
         {"target": schedule_target(SCHEDULE), "session": None},
         {"target": schedule_target(SCHEDULE), "cursor": SCHEDULE}],
    ),
])
def test_query_schemas_and_parsers_reject_cross_branch_fields(schema, parse, valid, invalid):
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    for arguments in valid:
        assert validator.is_valid(arguments), arguments
        parse(arguments)
    for arguments in invalid:
        assert not validator.is_valid(arguments), arguments
        with pytest.raises(ValueError):
            parse(arguments)


def test_capability_owner_rejects_mixed_query_before_observing_runtime(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("an invalid branch must not observe a runtime")

    monkeypatch.setattr("pulsara_agent.conversation_kernel.capability_query.runtime_rows", unexpected)
    query = CapabilitySourceQuery(None, runtime=None, plan=None, scope=ModelInputScopeKind.ROOT)
    with pytest.raises(CapabilityQueryError):
        asyncio.run(query.execute({"target": {"kind": "MCP_SERVER", "runtime_server_id": "s"}, "offset": 0}))


def test_collaboration_query_dispatch_preserves_scope_pages_and_work_details():
    async def run():
        work_row = {"agent_id": AGENT, "work_id": WORK, "instruction": "Exact work", "status": "completed", "final": "Exact final"}
        repository = NS(list_agent_sessions=object(), list_agent_works=object(), query_agent_work=object())
        calls = []

        async def read(method, **values):
            calls.append((method, values))
            assert values["root_session_id"] == ROOT
            if method is repository.list_agent_sessions:
                return [{"agent_id": AGENT}, {"agent_id": OTHER}]
            if method is repository.list_agent_works:
                if values["agent_id"] != AGENT:
                    return None
                return [work_row]
            assert method is repository.query_agent_work
            return work_row if values["work_id"] == WORK else None

        port = CollaborationToolPort(repository=repository, root_session_id=ROOT, session_id=ROOT,
                                     runtime=NS(read_deadline=lambda: 100), admission_facts=None, io=NS(run=read))
        context = NS(session_id=ROOT, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
        async def query(arguments):
            result = await port.invoke(tool_name="query_agents", arguments=arguments, invocation_context=context)
            return json.loads(result.content)

        page = await query({"max_items": 1})
        assert page == {"agents": [{"agent": agent_target(AGENT), "name": None}], "next_cursor": AGENT}
        assert calls[-1][1]["limit"] == 2
        works = await query({"target": agent_target(AGENT), "cursor": WORK, "max_items": 1})
        assert works["works"][0]["final"] == "Exact final"
        assert calls[-1][1]["cursor"] == WORK
        exact = await query({"target": work_target(WORK)})
        assert exact["instruction"] == "Exact work" and exact["final"] == "Exact final"
        with pytest.raises(ValueError, match="AGENT does not exist"):
            await query({"target": agent_target(OTHER)})
        before = len(calls)
        for name in ("list_agents", "inspect_agent"):
            with pytest.raises(ValueError, match="unknown collaboration"):
                await port.invoke(tool_name=name, arguments={}, invocation_context=context)
        with pytest.raises(ValueError, match="ROOT"):
            await port.invoke(tool_name="query_agents", arguments={}, invocation_context=NS(session_id=AGENT, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS))
        assert len(calls) == before

    asyncio.run(run())


def test_schedule_read_defaults_and_write_nulls_keep_independent_owners():
    async def run():
        service = ScheduledTaskService(NS())
        row = {"id": SCHEDULE, "session_id": ROOT, **VALUES, "revision": 3, "status": "PAUSED"}
        service.list = AsyncMock(return_value={"tasks": [row], "next_cursor": SCHEDULE})
        service.get = AsyncMock(return_value=row)
        service.create = AsyncMock(return_value=row)
        options = {"default_session_id": ROOT, "permission_mode": PermissionMode.BYPASS_PERMISSIONS}
        for arguments in ({}, {"session": None}):
            page = await service.invoke_query(arguments, **options)
            service.list.assert_awaited_with(session_id=ROOT)
            assert page["tasks"][0] == {"target": schedule_target(SCHEDULE), "session": {"kind": "SESSION", "session_id": ROOT}, **{k: v for k, v in row.items() if k not in {"id", "session_id"}}}
        await service.invoke_query({"session": {"kind": "SESSION", "session_id": OTHER}, "status": "ACTIVE", "cursor": SCHEDULE}, **options)
        service.list.assert_awaited_with(session_id=OTHER, status="ACTIVE", cursor=SCHEDULE)
        exact = await service.invoke_query({"target": schedule_target(SCHEDULE)}, **options)
        assert exact["revision"] == 3
        service.get.assert_awaited_once_with(SCHEDULE)
        for status in ({}, {"status": None}):
            request = {"action": "create", "values": VALUES, "session": None, **status}
            assert Draft202012Validator(manage_schedule_schema()).is_valid(request)
            await service.invoke_manage(request, **options)
            service.create.assert_awaited_with(values=VALUES, session_id=ROOT, **status)
            assert creation_status(status.get("status")) == "PAUSED"
        reads = service.list.await_count
        with pytest.raises(ScheduledTaskError):
            await service.invoke_query({"status": None}, **options)
        assert service.list.await_count == reads
        for action in ("list", "get"):
            with pytest.raises(ScheduledTaskError):
                validate_action({"action": action})
        with pytest.raises(ScheduledTaskError) as error:
            await service.invoke_manage({"action": "create", "values": {**VALUES, "permission_mode": "bypass-permissions"}},
                                        default_session_id=ROOT, permission_mode=PermissionMode.ACCEPT_EDITS)
        assert error.value.code == "PERMISSION_ESCALATION"

    asyncio.run(run())


@pytest.mark.parametrize("arguments,expected", [
    ({"session": {"kind": "SESSION", "session_id": ""}}, "INVALID_ID"),
    ({"cursor": ""}, "INVALID_ID"),
    ({"status": "INVALID"}, "INVALID_STATUS"),
    ({"status": None}, "INVALID_REQUEST"),
    ({"target": schedule_target(SCHEDULE), "session": None}, "INVALID_REQUEST"),
])
def test_schedule_query_domain_errors_precede_schema_without_weakening_branches(arguments, expected):
    with pytest.raises(ScheduledTaskError) as error:
        validate_query(arguments)
    assert error.value.code == expected


@pytest.mark.parametrize("arguments,expected,message", [
    ({"action": "update", "target": schedule_target(""), "expected_revision": 1, "values": {}}, "INVALID_ID", "任务身份字段无效"),
    ({"action": "pause", "target": schedule_target(SCHEDULE), "expected_revision": True}, "INVALID_REQUEST", "任务版本无效"),
    ({"action": "create", "values": VALUES, "status": "INVALID"}, "INVALID_REQUEST", "创建状态须为"),
    ({"action": "switch", "target": schedule_target(SCHEDULE), "expected_revision": 1, "replacement": None}, "INVALID_REQUEST", "切换目标无效"),
    ({"action": "switch", "target": schedule_target(SCHEDULE), "expected_revision": 1, "replacement": {"kind": "existing"}}, "INVALID_REQUEST", "切换目标字段无效"),
    ({"action": "update", "target": schedule_target(SCHEDULE), "expected_revision": 1, "values": {}}, "INVALID_REQUEST", "任务操作字段无效"),
])
def test_schedule_management_preserves_domain_diagnostics_and_complete_schema(arguments, expected, message):
    with pytest.raises(ScheduledTaskError, match=message) as error:
        validate_action(arguments)
    assert error.value.code == expected


@pytest.mark.parametrize("query,expected", [
    ({"session_id": ""}, "INVALID_ID"),
    ({"cursor": ""}, "INVALID_ID"),
    ({"status": "INVALID"}, "INVALID_STATUS"),
])
def test_http_schedule_query_preserves_identity_and_status_error_codes(query, expected):
    from pulsara_agent.desktop_app.scheduled_controller import ScheduledTaskController

    async def run():
        service = NS(list=AsyncMock())
        with pytest.raises(ScheduledTaskError) as error:
            await ScheduledTaskController(service).list(NS(query=query))
        assert error.value.code == expected
        service.list.assert_not_awaited()

    asyncio.run(run())
