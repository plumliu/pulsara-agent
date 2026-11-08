"""Model identity handoffs, native scope isolation and scheduled input sources."""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
import json
from types import SimpleNamespace as NS
from unittest.mock import ANY, AsyncMock

import pytest

from pulsara_agent.identity import derive_id
from pulsara_agent.llm.input import LLMTextPart
from pulsara_agent.model_input.contracts import FrozenProviderInputItemKind
from pulsara_agent.model_input.lowering import lower_canonical_item
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.tool_result_budget import ToolResultPageBudget, ToolResultPageBoundary
from pulsara_agent.scheduling.contracts import ScheduledProvenance, ScheduledTaskError, ScheduledTriggerSource
from pulsara_agent.scheduling.requests import schedule_target, validate_query
from pulsara_agent.scheduling.service import ScheduledTaskService, model_management_receipt


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
    return method(request, default_session_id=SESSION, permission_mode=mode,
                  page_budget=ToolResultPageBudget(tool_call_id=derive_id("call", "test", "schedule")))


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
        assert page["next"] == {"tool": "query_schedules", "arguments": {
            "session": {"kind": "SESSION", "session_id": SESSION}, "cursor": SOURCE}}
        found = await invoke(service, {"target": item["target"]})
        assert found["target"] == item["target"]
        assert "prompt" not in item
        assert found["prompt"] == VALUES["prompt"]
        edited = {**VALUES, "prompt": "Review and report the failing checks."}
        updated = await invoke(service, {"action": "update", "target": found["target"], "expected_revision": found["revision"], "values": edited})
        service.mutate.assert_awaited_once_with(task_id=SOURCE, expected_revision=7, action="update", values=edited, result_check=ANY)
        assert updated["revision"] == 8
        service.mutate.return_value = None
        assert await invoke(service, {"action": "delete", "target": updated["target"], "expected_revision": updated["revision"]}) == {
            "action": "delete", "target": updated["target"], "deleted": True}
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


def test_model_page_uses_budget_and_complete_resolved_next_without_prompt():
    async def run():
        service = ScheduledTaskService(NS())
        service.list = AsyncMock(return_value={"tasks": [row(prompt="\\\"中" * 30_000), row(DESTINATION)], "next_cursor": None})
        budget = ToolResultPageBudget(tool_call_id=derive_id("call", "test", "schedule-page"),
            accepts_provider_body=lambda body, _: len(json.loads(body)["tasks"]) <= 1)
        page = await service.invoke_query({"status": "PAUSED"}, default_session_id=SESSION,
            permission_mode=PermissionMode.BYPASS_PERMISSIONS, page_budget=budget)
        assert len(page["tasks"]) == 1
        assert "prompt" not in page["tasks"][0]
        assert page["tasks"][0]["inspect"] == {"tool": "query_schedules", "arguments": {"target": schedule_target(SOURCE)}}
        assert page["next"] == {"tool": "query_schedules", "arguments": {
            "session": {"kind": "SESSION", "session_id": SESSION}, "status": "PAUSED", "cursor": SOURCE}}
        service.list.assert_awaited_once_with(status="PAUSED", session_id=SESSION, model_summary=True)
        budget = ToolResultPageBudget(tool_call_id=derive_id("call", "test", "schedule-no-fit"),
            accepts_provider_body=lambda *_: False)
        with pytest.raises(ToolResultPageBoundary):
            await service.invoke_query({}, default_session_id=SESSION,
                permission_mode=PermissionMode.BYPASS_PERMISSIONS, page_budget=budget)
    asyncio.run(run())


@pytest.mark.parametrize("action", ["pause", "delete", "resume", "update"])
def test_schedule_mutation_checks_exact_receipt_before_writes(action):
    from datetime import datetime, timezone
    from pulsara_agent.conversation_kernel._repository.scheduling import _ScheduledTaskOperations

    operation = _ScheduledTaskOperations()
    source = row(status="ACTIVE" if action == "pause" else "PAUSED", next_run_at=None)
    writes = []
    class Connection:
        def execute(self, sql, arguments):
            assert sql.startswith("SELECT"), "budget must be checked before any SQL mutation"
            return NS(fetchone=lambda: None if sql.startswith("SELECT id ") else source)
    @contextmanager
    def transaction(**kwargs):
        yield Connection(), {"database_now": datetime(2029, 1, 1, tzinfo=timezone.utc)}
    operation._scheduled_transaction = transaction
    operation._cancel_scheduled_pending_in_connection = lambda *_: writes.append("cancel")
    operation._delete_scheduled_task_in_connection = lambda *_: writes.append("delete")
    def reject(payload):
        assert payload["id"] == SOURCE
        assert payload["revision"] in (7, 8)
        raise ToolResultPageBoundary("no receipt allowance")
    with pytest.raises(ToolResultPageBoundary):
        operation.mutate_scheduled_task(task_id=SOURCE, session_id=SESSION, memory_domain_id="test",
            guard=None, expected_revision=7, action=action, values={**VALUES, "prompt": "updated"},
            result_check=reject, deadline_monotonic=1e20)
    assert not writes


@pytest.mark.parametrize("action,replacement_kind", [("create", None), ("switch", "existing"), ("switch", "create")])
def test_schedule_allocation_and_switch_quote_real_targets_before_first_effect(monkeypatch, action, replacement_kind):
    from datetime import datetime, timezone
    from pulsara_agent.conversation_kernel._repository import scheduling

    operation = scheduling._ScheduledTaskOperations()
    source = row(status="ACTIVE", next_run_at=None)
    destination = row(DESTINATION, revision=2, next_run_at=None)
    class Connection:
        def execute(self, sql, arguments):
            assert sql.startswith("SELECT"), "a rejected receipt must precede SQL effects"
            return NS(fetchone=lambda: source if arguments[0] == SOURCE else destination)
    @contextmanager
    def transaction(**kwargs):
        yield Connection(), {"database_now": datetime(2029, 1, 1, tzinfo=timezone.utc), "model_call_binding": {}}
    operation._scheduled_transaction = transaction
    operation._cancel_scheduled_pending_in_connection = lambda *_: pytest.fail("must quote before cancelling pending inputs")
    monkeypatch.setattr(scheduling, "model_call_binding_from_dict", lambda _: object())
    def reject(payload):
        receipt = model_management_receipt(action, payload)
        if action == "switch":
            assert receipt["paused"]["target"] == schedule_target(SOURCE)
            assert receipt["active"]["target"]["kind"] == "SCHEDULE"
            assert receipt["active"]["status"] == "ACTIVE"
        else:
            assert receipt["target"]["kind"] == "SCHEDULE"
            assert receipt["revision"] == 1
        raise ToolResultPageBoundary("no receipt allowance")
    common = {"session_id": SESSION, "memory_domain_id": "test", "guard": None,
        "model_resolution_snapshot": NS(reconcile=lambda _: None), "result_check": reject, "deadline_monotonic": 1e20}
    with pytest.raises(ToolResultPageBoundary):
        if action == "create":
            operation.create_scheduled_task(**common, values=VALUES)
        else:
            replacement = ({"kind": "create", "values": VALUES} if replacement_kind == "create" else {
                "kind": "existing", "target": schedule_target(DESTINATION), "expected_revision": 2})
            operation.switch_scheduled_task(**common, task_id=SOURCE, expected_revision=7, replacement=replacement)


def test_host_message_receipt_quotes_the_work_locked_by_the_message_owner():
    from pulsara_agent.conversation_kernel.host import KernelHostCore

    async def run():
        work = derive_id("queue", "test", "message-work")
        effects, quoted = [], []
        def enqueue(*args, result_check, **kwargs):
            result_check(work)
            effects.append("message")
            return {"work_id": work}
        async def io(method, *args, **kwargs):
            return method(*args, **kwargs)
        live = NS(_lock=asyncio.Lock(), active_root_turn_id=lambda: derive_id("turn", "test", "message-turn"),
            _io=NS(run=io), repository=NS(enqueue_agent_steer=enqueue), _lease=NS(guard=object()),
            _canonical_deadline=lambda: 1e20, wake_queued_inputs=lambda: effects.append("wake"))
        core = NS(_live_agent_session=lambda _: live, _agent_runtime=NS(notify_changed=AsyncMock()))
        def deny(body, _):
            quoted.append(json.loads(body))
            return False
        context = NS(session_id=SESSION, work_id=None,
            page_budget=ToolResultPageBudget(tool_call_id=derive_id("call", "test", "message-quote"), accepts_provider_body=deny))
        with pytest.raises(ToolResultPageBoundary):
            await KernelHostCore._send_agent_message(core, SESSION, "must not be sent", context)
        assert quoted[0]["work"] == {"kind": "WORK", "work_id": work}
        assert quoted[0]["delivery"] == "queued"
        assert not effects
        core._agent_runtime.notify_changed.assert_not_awaited()
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
        service.switch.assert_awaited_once_with(task_id=SOURCE, expected_revision=7, replacement=replacement, result_check=ANY)
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
def test_scheduled_original_header_carries_copyable_identity(source):
    provenance = ScheduledProvenance(SOURCE, 7, "2030-01-01T01:00:00+00:00", source)
    content = (LLMTextPart("Exact saved instruction."),)
    item = NS(item_kind=FrozenProviderInputItemKind.USER, content=content, scheduled_input=provenance)
    lowered = lower_canonical_item(item)
    assert lowered.fixed_message is not None
    header = lowered.fixed_message.content[0].text
    target, _ = json.JSONDecoder().raw_decode(header[header.index('{'):])
    validate_query({"target": target})
    assert target == schedule_target(SOURCE)
    assert provenance.due_at_utc in header
    assert ("Automatic timer" in header) == (source is ScheduledTriggerSource.TIMER)
    assert ("User selected Run once" in header) == (source is ScheduledTriggerSource.RUN_ONCE)
    assert "expected_revision" not in header
    assert lowered.fixed_message.content[1:] == content
    assert provenance.task_revision == 7
