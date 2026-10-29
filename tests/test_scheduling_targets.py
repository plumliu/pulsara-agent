"""Model identity handoffs, native scope isolation and scheduled input sources."""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from pulsara_agent.identity import derive_id
from pulsara_agent.llm.input import LLMTextPart
from pulsara_agent.model_input.contracts import FrozenProviderInputItemKind, StructuredModelInputLimits
from pulsara_agent.model_input.lowering import lower_canonical_item, lower_retained_request_content
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.scheduling.contracts import ScheduledProvenance, ScheduledTaskError, ScheduledTriggerSource
from pulsara_agent.scheduling.requests import schedule_target, validate_query
from pulsara_agent.scheduling.service import ScheduledTaskService


SESSION = derive_id("session", "test", "scheduled-handoff")
SOURCE = derive_id("schedule", "test", "source")
DESTINATION = derive_id("schedule", "test", "destination")
VALUES = {
    "name": "Review", "prompt": "Review the latest changes.", "timezone": "Asia/Shanghai",
    "permission_mode": "read-only",
    "schedule": {"contract": "scheduled-rule:v1", "kind": "daily", "start_date": "2030-01-01", "time": "09:00"},
}


def row(task_id=SOURCE, **changes):
    return {"id": task_id, "session_id": SESSION, **deepcopy(VALUES), "revision": 7, "status": "PAUSED", **changes}


def invoke(service, request, mode=PermissionMode.BYPASS_PERMISSIONS):
    method = service.invoke_manage if "action" in request else service.invoke_query
    return method(request, default_session_id=SESSION, permission_mode=mode)


def test_list_target_can_be_copied_to_get_update_and_delete_without_id_mirrors():
    async def run():
        service = ScheduledTaskService(NS())
        original = row()
        service.list = AsyncMock(return_value={"tasks": [original], "next_cursor": SOURCE})
        service.get = AsyncMock(return_value=original)
        service.mutate = AsyncMock(return_value=row(revision=8))
        page = await invoke(service, {"session": None})
        item = page["tasks"][0]
        assert "id" not in item and "session_id" not in item
        assert item["session"] == {"kind": "SESSION", "session_id": SESSION}
        assert page["next_cursor"] == SOURCE
        found = await invoke(service, {"target": item["target"]})
        assert found == item
        edited = {**VALUES, "prompt": "Review and report the failing checks."}
        updated = await invoke(service, {"action": "update", "target": found["target"], "expected_revision": found["revision"], "values": edited})
        service.mutate.assert_awaited_once_with(task_id=SOURCE, expected_revision=7, action="update", values=edited)
        assert updated["revision"] == 8
        service.mutate.return_value = None
        assert await invoke(service, {"action": "delete", "target": updated["target"], "expected_revision": updated["revision"]}) is None
        assert original == row()
    asyncio.run(run())


def test_http_scope_is_user_level_and_null_creation_parent_is_not_a_model_default():
    from pulsara_agent.desktop_app.scheduled_controller import ScheduledTaskController

    async def run():
        service = NS(list=AsyncMock(return_value={"tasks": [], "next_cursor": None}), create=AsyncMock())
        controller = ScheduledTaskController(service)
        await controller.list(NS(query={}))
        service.list.assert_awaited_once_with(cursor=None, status=None, session_id=None)
        with pytest.raises(ScheduledTaskError):
            await controller.create(NS(json=AsyncMock(return_value={"session_id": None, "values": VALUES})))
        service.create.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["existing", "create"])
def test_switch_keeps_source_and_destination_guards_and_checks_destination_permissions(kind):
    async def run():
        service = ScheduledTaskService(NS())
        service.get = AsyncMock(return_value=row(DESTINATION, permission_mode="bypass-permissions"))
        service.switch = AsyncMock(return_value={"paused": row(), "active": row(DESTINATION, revision=2, status="ACTIVE")})
        replacement = (
            {"kind": "existing", "target": schedule_target(DESTINATION), "expected_revision": 1}
            if kind == "existing" else {"kind": "create", "values": {**VALUES, "permission_mode": "bypass-permissions"}}
        )
        request = {"action": "switch", "target": schedule_target(SOURCE), "expected_revision": 7, "replacement": replacement}
        with pytest.raises(ScheduledTaskError) as error:
            await invoke(service, request, PermissionMode.ACCEPT_EDITS)
        assert error.value.code == "PERMISSION_ESCALATION"
        service.switch.assert_not_awaited()
        result = await invoke(service, request)
        service.switch.assert_awaited_once_with(task_id=SOURCE, expected_revision=7, replacement=replacement)
        assert result["paused"]["target"] == schedule_target(SOURCE)
        assert result["active"]["target"] == schedule_target(DESTINATION)
        assert result["active"]["revision"] == 2
        if kind == "existing":
            service.get.assert_awaited_with(DESTINATION)
        else:
            service.get.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("arguments", [
    {"action": "get", "task_id": SOURCE},
    {"action": "list", "session_id": SESSION},
    {"action": "get", "target": {"kind": "SESSION", "session_id": SESSION}},
    {"action": "get", "target": {**schedule_target(SOURCE), "expected_revision": 7}},
    {"action": "pause", "target": schedule_target(SOURCE), "expected_revision": True},
    {"action": "pause", "target": schedule_target(SOURCE), "expected_revision": 7, "session": None},
    {"action": "switch", "target": schedule_target(SOURCE), "expected_revision": 7, "replacement": {"kind": "existing", "target": schedule_target(SOURCE), "expected_revision": 7}},
    {"action": "switch", "target": schedule_target(SOURCE), "expected_revision": 7, "replacement": {"kind": "existing", "task_id": DESTINATION, "expected_revision": 7}},
])
def test_wrong_identity_or_old_request_is_rejected_before_native_service(arguments):
    async def run():
        service = ScheduledTaskService(NS())
        service.get = AsyncMock()
        service.list = AsyncMock()
        service.mutate = AsyncMock()
        service.switch = AsyncMock()
        with pytest.raises(ScheduledTaskError):
            await invoke(service, arguments)
        for operation in (service.get, service.list, service.mutate, service.switch):
            operation.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("source", list(ScheduledTriggerSource))
def test_scheduled_header_carries_copyable_identity_in_normal_and_retained_requests(source):
    provenance = ScheduledProvenance(SOURCE, 7, "2030-01-01T01:00:00+00:00", source)
    content = (LLMTextPart("Exact saved instruction."),)
    item = NS(item_kind=FrozenProviderInputItemKind.USER, content=content, scheduled_input=provenance)
    lowered = lower_canonical_item(item, artifact_read_available=False, limits=StructuredModelInputLimits())
    retained = lower_retained_request_content(NS(item_kind=item.item_kind, content=NS(parts=content), scheduled_input=provenance))
    assert lowered.fixed_message.content == retained
    header = retained[0].text
    target, _ = json.JSONDecoder().raw_decode(header[header.index('{'):])
    validate_query({"target": target})
    assert target == schedule_target(SOURCE)
    assert provenance.due_at_utc in header
    assert ("Automatic timer" in header) == (source is ScheduledTriggerSource.TIMER)
    assert ("User selected Run once" in header) == (source is ScheduledTriggerSource.RUN_ONCE)
    assert "expected_revision" not in header
    assert retained[1:] == content
    assert provenance.task_revision == 7
