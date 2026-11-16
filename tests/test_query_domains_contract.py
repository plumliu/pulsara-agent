"""Current query branches retain exact scope and independent domain owners."""

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from jsonschema import Draft202012Validator
from pulsara_agent.capability.source_query import parse_query, query_input_schema
from pulsara_agent.conversation_kernel.capability_query import CapabilitySourceQuery
from pulsara_agent.conversation_kernel.subagents.collaboration_api import (
    CollaborationToolPort,
)
from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import (
    agent_target,
    work_target,
    query_agents_input_schema,
    validate_collaboration_parameters,
)
from pulsara_agent.identity import derive_id
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.scheduling.contracts import ScheduledTaskError
from pulsara_agent.scheduling.requests import (
    creation_status,
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
    "schedule": {"kind": "daily", "start_date": "2030-01-01", "time": "09:00"},
}


@pytest.mark.parametrize(
    "schema,parse,valid,invalid",
    [
        (
            query_input_schema(),
            parse_query,
            [
                {"kind": "MCP"},
                {"kind": "PLUGIN"},
                {"kind": "SKILL", "scope": "USER"},
                {
                    "kind": "MCP",
                    "target": {
                        "kind": "MCP_TOOL",
                        "server_id":"docs", "remote_tool_name":"read",
                    },
                },
            ],
            [{}, {"kind": "MCP", "limit": 1}, {"kind": "MCP", "target": None}],
        ),
        (
            query_agents_input_schema(),
            lambda a: validate_collaboration_parameters("query_agents", a),
            [{}, {"target": agent_target(AGENT)}, {"target": work_target(WORK)}],
            [
                {"target": None},
                {"max_items": 50},
                {"target": work_target(WORK), "cursor": WORK},
            ],
        ),
        (
            query_schedules_schema(),
            validate_query,
            [{}, {"status": "ALL"}, {"target": schedule_target(SCHEDULE)}],
            [
                {"session": None},
                {"status": None},
                {"target": schedule_target(SCHEDULE), "status": "ACTIVE"},
            ],
        ),
    ],
)
def test_query_schemas_and_parsers_reject_cross_branch_fields(
    schema, parse, valid, invalid
):
    validator = Draft202012Validator(schema)
    for args in valid:
        assert validator.is_valid(args)
        parse(args)
    for args in invalid:
        assert not validator.is_valid(args)
        with pytest.raises(ValueError):
            parse(args)


def test_capability_owner_rejects_mixed_query_before_observing_runtime(monkeypatch):
    query = CapabilitySourceQuery(
        None, runtime=None, mcp_configs=(), scope=ModelInputScopeKind.ROOT, inspect_tool=None
    )

    async def unexpected(*args):
        pytest.fail("invalid query must not inspect runtime")

    monkeypatch.setattr(query, "_mcp", unexpected)
    result = asyncio.run(
        query.execute(
            {
                "kind": "MCP",
                "target": {
                    "kind": "MCP_CONNECTION",
                    "config_path": "/exact/mcp.yaml",
                    "server_id": "docs",
                },
                "scope": "USER",
            }
        )
    )
    assert result["meta"]["status"] == "ERROR"


def test_collaboration_query_dispatch_preserves_scope_and_complete_work_details():
    async def run():
        row = {
            "agent_id": AGENT,
            "work_id": WORK,
            "work_title": "Exact title",
            "instruction": "Exact work",
            "status": "completed",
            "final": "Exact final",
        }
        repository = NS(
            list_query_agent_sessions=object(),
            list_query_agent_works=object(),
            query_visible_agent_work=object(),
        )
        calls = []

        async def read(method, **values):
            calls.append((method, values))
            assert values["root_session_id"] == ROOT and values[
                "observer_session_id"
            ] in {ROOT, AGENT}
            if method is repository.list_query_agent_sessions:
                return [
                    {
                        "agent_id": AGENT,
                        "title": "Ada Lovelace",
                        "ui_current_work_id": WORK,
                        "ui_current_work_title": "Exact title",
                        "ui_queued_work_count": 2,
                    }
                ]
            if method is repository.list_query_agent_works:
                if values["agent_id"] != AGENT:
                    raise ValueError("AGENT does not exist")
                return [row]
            return row if values["work_id"] == WORK else None

        port = CollaborationToolPort(
            repository=repository,
            root_session_id=ROOT,
            session_id=ROOT,
            runtime=NS(read_deadline=lambda: 100),
            admission_facts=None,
            io=NS(run=read),
        )
        context = NS(
            session_id=ROOT,
            effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            code_cell_id=None,
        )

        async def query(arguments):
            result = await port.invoke(
                tool_name="query_agents",
                arguments=arguments,
                invocation_context=context,
            )
            return [p.value for p in result.content.content if p.type == "object"]

        listed = await query({})
        assert listed[0]["target"] == agent_target(AGENT) and listed[0][
            "current_work"
        ] == work_target(WORK)
        assert calls[-1][1]["limit"] is None
        works = await query({"target": agent_target(AGENT)})
        assert works[0]["work_title"] == "Exact title" and "final" not in works[0]
        exact = await query({"target": work_target(WORK)})
        assert (
            exact[0]["instruction"] == "Exact work"
            and exact[0]["final"] == "Exact final"
        )
        with pytest.raises(ValueError):
            await query({"target": agent_target(OTHER)})
        context.session_id = AGENT
        await query({})
        assert calls[-1][1]["observer_session_id"] == AGENT

    asyncio.run(run())


def test_schedule_queries_are_current_session_and_private_revisions_follow_the_real_rows():
    async def run():
        service = ScheduledTaskService(NS())
        row = {
            "id": SCHEDULE,
            "session_id": ROOT,
            **VALUES,
            "permission_mode": "read-only",
            "revision": 3,
            "status": "PAUSED",
            "next_run_at": None,
        }
        service.get = AsyncMock(return_value=row)
        service.create = AsyncMock(return_value=row)
        options = {
            "default_session_id": ROOT,
            "permission_mode": PermissionMode.READ_ONLY,
            "memory_domain_id": "test-domain",
        }
        exact = await service.invoke_query(
            {"target": schedule_target(SCHEDULE)}, **options
        )
        assert exact.value.content[0].value["target"] == schedule_target(SCHEDULE)
        assert exact.value.content[0].value["prompt"] == "Review the current changes."
        assert exact.result_facts["schedule_revisions"] == {SCHEDULE: 3}
        for status in ({}, {"status": "PAUSED"}):
            result = await service.invoke_manage(
                {"action": "create", "values": VALUES, **status}, **options
            )
            service.create.assert_awaited_with(
                session_id=ROOT,
                values={**VALUES, "permission_mode": "read-only"},
                status=status.get("status"),
            )
            assert result.value.public_fields["target"] == schedule_target(SCHEDULE)
            assert creation_status(status.get("status")) == "PAUSED"
        service.get.return_value = {**row, "session_id": OTHER}
        with pytest.raises(ScheduledTaskError, match="不属于当前会话"):
            await service.invoke_query({"target": schedule_target(SCHEDULE)}, **options)
        with pytest.raises(ScheduledTaskError):
            await service.invoke_manage(
                {
                    "action": "create",
                    "values": {**VALUES, "permission_mode": "bypass-permissions"},
                },
                **options,
            )

    asyncio.run(run())


@pytest.mark.parametrize(
    "args",
    [
        {"status": None},
        {"status": "INVALID"},
        {"target": schedule_target(SCHEDULE), "session": None},
        {"target": schedule_target(SCHEDULE), "cursor": SCHEDULE},
    ],
)
def test_schedule_query_rejects_cross_branch_fields(args):
    with pytest.raises(ScheduledTaskError) as error:
        validate_query(args)
    assert error.value.code == "INVALID_REQUEST"


@pytest.mark.parametrize(
    "args",
    [
        {"action": "update", "target": schedule_target(SCHEDULE), "values": {}},
        {
            "action": "pause",
            "target": schedule_target(SCHEDULE),
            "expected_revision": 1,
        },
        {"action": "create", "values": VALUES, "status": None},
        {
            "action": "switch",
            "target": schedule_target(SCHEDULE),
            "replacement": {"kind": "existing"},
        },
        {
            "action": "switch",
            "target": schedule_target(SCHEDULE),
            "replacement": {"kind": "existing", "target": schedule_target(SCHEDULE)},
        },
    ],
)
def test_schedule_management_preserves_closed_domain_validation(args):
    with pytest.raises(ScheduledTaskError):
        validate_action(args)


@pytest.mark.parametrize(
    "query,expected",
    [
        ({"session_id": ""}, "INVALID_ID"),
        ({"cursor": ""}, "INVALID_ID"),
        ({"status": "INVALID"}, "INVALID_STATUS"),
    ],
)
def test_http_schedule_query_preserves_identity_and_status_error_codes(query, expected):
    from pulsara_agent.desktop_app.scheduled_controller import ScheduledTaskController

    async def run():
        service = NS(list=AsyncMock())
        with pytest.raises(ScheduledTaskError) as error:
            await ScheduledTaskController(service).list(NS(query=query))
        assert error.value.code == expected
        service.list.assert_not_awaited()

    asyncio.run(run())
