"""Actual schedule rows freeze public observations with matching internal revisions."""
import asyncio
from datetime import datetime, timezone
from time import monotonic
from types import SimpleNamespace as NS

import pytest

from pulsara_agent.conversation_kernel.repository import AssistantToolCallBlock, ConversationKernelRepository
from pulsara_agent.conversation_kernel.contracts import PromptDeliveryMode
from pulsara_agent.conversation_kernel.tool_outputs import PostgresToolOutputReadPort, project_tool_result_export
from pulsara_agent.conversation_kernel.tool_runtime_management import _ToolManagementOperations
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import FrozenPromptContent
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.ports.tool_execution import thaw_tool_json_object
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from pulsara_agent.scheduling.requests import schedule_target
from pulsara_agent.scheduling.service import ScheduledTaskService
from pulsara_agent.scheduling.contracts import ScheduledTaskError
from tests.support.model_config import (
    acquire_bound_test_writer, enqueue_test_prompt,
    test_model_resolution_snapshot as model_resolution_snapshot,
)
from tests.support.postgres import verified_postgres_provider
from tests.test_hardcut_entry_parts import open_turn, read
from tests.test_hardcut_tool_settlement import executor

pytestmark = pytest.mark.postgres
VALUES = {"name": "Saved work", "prompt": "Keep this original task", "timezone": "Asia/Shanghai",
    "permission_mode": "read-only", "schedule": {"kind": "interval", "anchor_at_utc": "2020-01-01T00:00:00+00:00", "seconds": 900}}


@pytest.fixture
def repository(stage2_migrated_postgres_database):
    return ConversationKernelRepository(verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn))


def service_for(repository, lease):
    async def ensure():
        return repository
    async def apply(method, arguments):
        return getattr(repository, method)(guard=lease.guard, **arguments)
    async def host(_session_id, *, memory_domain_id):
        assert memory_domain_id == "u_local"
        return NS(apply_scheduled_management=apply)
    service = ScheduledTaskService(NS(_ensure_resources=ensure,
        _canonical_deadline=lambda: monotonic()+30, memory_domain_id="u_local",
        scheduled_management_host=host,
        model_runtime=NS(freeze_resolution_snapshot=model_resolution_snapshot)))
    reads = []
    original_read = service._read
    async def observed_read(method, **arguments):
        reads.append((method, arguments))
        return await original_read(method, **arguments)
    service._read = observed_read
    return service, reads


def admit(repository, lease, intent, cut, tool_name, arguments):
    assistant, attempt = random_id("entry"), random_id("attempt")
    call = "actual-schedule-call-" + assistant
    repository.commit_assistant_message(lease.guard, cut=cut, entry_id=assistant,
        blocks=(AssistantToolCallBlock(random_id("block"), call, tool_name, freeze_json(arguments)),),
        occurred_at=datetime.now(timezone.utc), actor_id="model:test", complete_turn=False,
        deadline_monotonic=monotonic()+30)
    with read(repository) as connection:
        row = connection.execute("SELECT workspace_id,permission_snapshot_fingerprint FROM pulsara_v3.turns WHERE id=%s", (intent.turn_id,)).fetchone()
    repository.accept_tool_attempt(lease.guard, attempt_id=attempt, assistant_entry_id=assistant,
        tool_call_id=call, authorization_kind="policy", authorization_reference="allow",
        actor_kind="runtime", actor_id="test", remote_idempotency_key=None, retry_of_attempt_id=None,
        permission_snapshot_fingerprint=row["permission_snapshot_fingerprint"],
        occurred_at=datetime.now(timezone.utc), deadline_monotonic=monotonic()+30)
    return NS(session_id=lease.guard.session_id, turn_id=intent.turn_id,
        workspace_id=row["workspace_id"], assistant_entry_id=assistant, tool_call_id=call,
        attempt_id=attempt, result_id=random_id("result"), result_entry_id=random_id("entry"))


async def produce(service, candidate, tool_name, arguments):
    owner = _ToolManagementOperations()
    owner._scheduled_tasks = service
    owner._session_id, owner._memory_domain_id = candidate.session_id, "u_local"
    return await owner._invoke_scheduled_management(tool_name=tool_name, arguments=arguments,
        tool_call_id=candidate.tool_call_id,
        invocation_context=NS(conversation_scope_kind="ROOT", effective_permission_mode=DEFAULT_PERMISSION_MODE),
        invocation_started=monotonic(), observation_origin=ToolObservationOrigin.BUILTIN)


async def accept(repository, lease, candidate, result, tool_name, arguments):
    execution = executor(repository, lease)
    return await execution._settle_known_tool_result(session_id=candidate.session_id,
        turn_id=candidate.turn_id, assistant_entry_id=candidate.assistant_entry_id,
        tool_name=tool_name, tool_call_id=candidate.tool_call_id, invocation_arguments=arguments,
        call_ordinal=0, scope_kind=ModelInputScopeKind.ROOT, result_id=candidate.result_id,
        result_entry_id=candidate.result_entry_id, attempt_id=candidate.attempt_id,
        workspace_id=candidate.workspace_id, result=result, observed_at=datetime.now(timezone.utc),
        observation_origin=ToolObservationOrigin.BUILTIN, live_sink=None,
        tool_result_block_id=random_id("block"), live_attribution={})


def saved(repository, candidate):
    value = PostgresToolOutputReadPort(repository.connection_provider,
        session_id=candidate.session_id, workspace_id=candidate.workspace_id).read_result(
            {"kind": "SESSION_ENTRY", "entry_id": candidate.result_entry_id}).value
    with read(repository) as connection:
        facts = connection.execute("SELECT result_facts FROM pulsara_v3.tool_results WHERE id=%s", (candidate.result_id,)).fetchone()["result_facts"]
    assert "schedule_revisions" not in value and "result_facts" not in value
    exported = project_tool_result_export(value)
    assert b"schedule_revisions" not in exported and b"result_facts" not in exported
    return thaw_tool_json_object(value), facts


@pytest.mark.parametrize("detail", [False, True])
def test_query_producer_freezes_matching_rows_without_a_facts_reread(repository, detail):
    async def run():
        lease, intent, _, cut = open_turn(repository, text="schedule query\n")
        service, reads = service_for(repository, lease)
        try:
            first = await service.create(session_id=lease.guard.session_id, values=VALUES)
            second = await service.create(session_id=lease.guard.session_id, values={**VALUES, "schedule": {**VALUES["schedule"], "seconds": 1800}})
            arguments = {"target": schedule_target(first["id"])} if detail else {}
            candidate = admit(repository, lease, intent, cut, "query_schedules", arguments)
            reads.clear()
            result = await produce(service, candidate, "query_schedules", arguments)
            assert [method for method, _ in reads] == (["read_scheduled_task"] if detail else ["list_model_scheduled_tasks"])
            expected = {first["id"]: first["revision"]}
            if not detail:
                expected[second["id"]] = second["revision"]
            assert result.result_facts == {"schedule_revisions": expected}
            # Later live configuration must not replace the query's original
            # config/revision pair while this known result is accepted.
            await service.mutate(task_id=first["id"], expected_revision=first["revision"],
                action="update", values={**VALUES, "schedule": {**VALUES["schedule"], "seconds": 3600}})
            read_count = len(reads)
            await accept(repository, lease, candidate, result, "query_schedules", arguments)
            value, facts = saved(repository, candidate)
            assert facts == {"schedule_revisions": expected}
            public = next(item["value"] for item in value["content"] if item["type"] == "object" and item["value"]["target"] == schedule_target(first["id"]))
            assert public["schedule"]["seconds"] == 900
            assert set(public) == {"target", "name", "prompt", "schedule", "timezone", "permission_mode", "status", "next_run_at"}
            assert len(reads) == read_count
        finally:
            await service.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("switch", [False, "existing", "create"])
def test_management_producer_freezes_actual_post_change_rows_including_both_switch_sides(repository, switch):
    async def run():
        lease, intent, _, cut = open_turn(repository, text="schedule management\n")
        service, reads = service_for(repository, lease)
        try:
            source = await service.create(session_id=lease.guard.session_id, values=VALUES, status="ACTIVE")
            destination = await service.create(session_id=lease.guard.session_id,
                values={**VALUES, "schedule": {**VALUES["schedule"], "seconds": 1800}})
            if switch:
                arguments = {"action": "switch", "target": schedule_target(source["id"]),
                    "replacement": ({"kind": "existing", "target": schedule_target(destination["id"])}
                        if switch == "existing" else {"kind": "create", "values": {key: val for key, val in {**VALUES, "schedule": {**VALUES["schedule"], "seconds": 1800}}.items() if key != "permission_mode"}})}
            else:
                arguments = {"action": "update", "target": schedule_target(source["id"]),
                    "values": {"schedule": {**VALUES["schedule"], "seconds": 3600}}}
            # The model's revision authority comes only from an actually accepted
            # complete query result in this session, never current live rows.
            query_arguments = {"status": "ALL"}
            query = admit(repository, lease, intent, cut, "query_schedules", query_arguments)
            query_result = await produce(service, query, "query_schedules", query_arguments)
            await accept(repository, lease, query, query_result, "query_schedules", query_arguments)
            cut = repository.prepare_provider_input_cut(lease.guard, turn_id=intent.turn_id, deadline_monotonic=monotonic()+30)
            candidate = admit(repository, lease, intent, cut, "manage_schedule", arguments)
            result = await produce(service, candidate, "manage_schedule", arguments)
            expected = {source["id"]: source["revision"]+1}
            if switch:
                active_id = result.content.public_fields["target"]["task_id"]
                expected[active_id] = destination["revision"]+1 if switch == "existing" else 1
            assert result.result_facts == {"schedule_revisions": expected}
            assert set(result.result_facts) == {"schedule_revisions"}
            # This independent edit happens after the returned management rows.
            changed_id = active_id if switch else source["id"]
            await service.mutate(task_id=changed_id, expected_revision=expected[changed_id], action="pause")
            read_count = len(reads)
            await accept(repository, lease, candidate, result, "manage_schedule", arguments)
            value, facts = saved(repository, candidate)
            assert facts == {"schedule_revisions": expected}
            if switch:
                assert value["target"] == schedule_target(active_id)
                assert value["meta"]["schedule_status"] == "ACTIVE"
                assert value["content"] == [{"type":"text", "value":"已暂停原计划并启用替换计划。"}]
            else:
                assert value["target"] == schedule_target(source["id"])
                assert value["content"] == [{"type":"text", "value":"已更新定时任务。"}]
            assert len(reads) == read_count
        finally:
            await service.aclose()
    asyncio.run(run())


def test_native_queries_are_session_scoped_and_management_uses_latest_saved_revision(repository):
    async def run():
        lease, intent, _, cut = open_turn(repository, text="private schedule authority\n")
        other_lease, _, _, _ = open_turn(repository, text="other schedule session\n")
        service, _ = service_for(repository, lease)
        other, _ = service_for(repository, other_lease)
        try:
            own = await service.create(session_id=lease.guard.session_id, values=VALUES)
            foreign = await other.create(session_id=other_lease.guard.session_id, values=VALUES)
            with pytest.raises(ScheduledTaskError, match="先用 query_schedules"):
                await service.invoke_manage({"action":"pause","target":schedule_target(own["id"])},
                    default_session_id=lease.guard.session_id, permission_mode=DEFAULT_PERMISSION_MODE)
            observed = await service.invoke_query({}, default_session_id=lease.guard.session_id,
                permission_mode=DEFAULT_PERMISSION_MODE)
            assert [part.value["target"] for part in observed.value.content] == [schedule_target(own["id"])]
            with pytest.raises(ScheduledTaskError, match="不属于当前会话"):
                await service.invoke_query({"target":schedule_target(foreign["id"])},
                    default_session_id=lease.guard.session_id, permission_mode=DEFAULT_PERMISSION_MODE)

            async def accept_query():
                nonlocal cut
                candidate = admit(repository, lease, intent, cut, "query_schedules", {})
                result = await produce(service, candidate, "query_schedules", {})
                await accept(repository, lease, candidate, result, "query_schedules", {})
                cut = repository.prepare_provider_input_cut(lease.guard, turn_id=intent.turn_id, deadline_monotonic=monotonic()+30)
                return candidate
            await accept_query()
            updated = await service.mutate(task_id=own["id"], expected_revision=own["revision"],
                action="update", values={"prompt":"User changed the saved prompt"})
            with pytest.raises(ScheduledTaskError, match="任务已修改，请重新加载"):
                await service.invoke_manage({"action":"delete","target":schedule_target(own["id"])},
                    default_session_id=lease.guard.session_id, permission_mode=DEFAULT_PERMISSION_MODE)
            await accept_query()
            paused = await service.invoke_manage({"action":"pause","target":schedule_target(own["id"])},
                default_session_id=lease.guard.session_id, permission_mode=DEFAULT_PERMISSION_MODE)
            assert paused.result_facts == {"schedule_revisions":{own["id"]:updated["revision"]}}
            actual = await service.get(own["id"])
            assert actual["prompt"] == "User changed the saved prompt"
            assert actual["permission_mode"] == VALUES["permission_mode"]
            assert actual["status"] == "PAUSED"
        finally:
            await service.aclose()
            await other.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("action", ["pause", "resume", "delete"])
@pytest.mark.parametrize("initial_status", ["ACTIVE", "PAUSED"])
@pytest.mark.parametrize("pending_inputs", [False, True])
def test_remaining_management_actions_use_the_saved_snapshot_without_public_revision(repository, action, initial_status, pending_inputs):
    async def run():
        lease = acquire_bound_test_writer(repository, session_id=random_id("session"),
            workspace_id=random_id("workspace"), writer_owner_id=random_id("host"),
            lease_seconds=300, deadline_monotonic=monotonic()+30)
        service, _ = service_for(repository, lease)
        try:
            task = await service.create(session_id=lease.guard.session_id, values=VALUES,
                status=initial_status)
            enqueue_test_prompt(repository, lease.guard, command_id=random_id("command"),
                queue_item_id=random_id("queue"), client_submission_id=random_id("command"),
                delivery_mode=PromptDeliveryMode.NEW_TURN, target_turn_id=None,
                permission_snapshot_id=random_id("permission"), requested_permission_mode=DEFAULT_PERMISSION_MODE,
                model_call_binding=None, content=FrozenPromptContent.text("schedule action\n"),
                occurred_at=datetime.now(timezone.utc), actor_id="test", deadline_monotonic=monotonic()+30)
            pending = []
            if pending_inputs:
                from tests.test_scheduled_tasks_postgres import admission, enqueue
                owner = repository, lease, model_resolution_snapshot()
                for _ in range(2):
                    queued, _ = enqueue(owner, admission(task, manual=True, command=random_id("command")))
                    pending.append(queued.queue_item_id)
            head = repository.prepare_prompt_head_consumption(session_id=lease.guard.session_id,
                occurred_at=datetime.now(timezone.utc), actor_id="test", deadline_monotonic=monotonic()+30)
            repository.consume_prepared_prompt_head(lease.guard, candidate=head, deadline_monotonic=monotonic()+30)
            intent = NS(turn_id=head.exact_turn_id)
            cut = repository.prepare_provider_input_cut(lease.guard, turn_id=intent.turn_id, deadline_monotonic=monotonic()+30)
            query = admit(repository, lease, intent, cut, "query_schedules", {})
            await accept(repository, lease, query, await produce(service, query, "query_schedules", {}), "query_schedules", {})
            cut = repository.prepare_provider_input_cut(lease.guard, turn_id=intent.turn_id, deadline_monotonic=monotonic()+30)
            arguments = {"action":action,"target":schedule_target(task["id"])}
            candidate = admit(repository, lease, intent, cut, "manage_schedule", arguments)
            result = await produce(service, candidate, "manage_schedule", arguments)
            await accept(repository, lease, candidate, result, "manage_schedule", arguments)
            value, facts = saved(repository, candidate)
            expected = {"meta": {"status": "SUCCESS"}, "content": [{"type": "text", "value": {
                "pause": "已暂停定时任务。", "resume": "已恢复定时任务。", "delete": "已删除定时任务。",
            }[action]}]}
            if action != "delete":
                expected["target"] = schedule_target(task["id"])
            if pending_inputs and action in {"pause", "delete"}:
                expected["content"][0]["value"] = {
                    "pause": "已暂停定时任务，并取消其待执行输入。",
                    "delete": "已删除定时任务，并取消其待执行输入。",
                }[action]
            assert value == expected
            assert set(facts["schedule_revisions"]) == {task["id"]}
            with read(repository) as connection:
                states = connection.execute(
                    "SELECT status FROM pulsara_v3.prompt_queue_items WHERE id=ANY(%s)", (pending,),
                ).fetchall()
                assert [row["status"] for row in states] == [
                    "PENDING" if action == "resume" else "CANCELLED"
                ] * len(pending)
                assert connection.execute("SELECT status FROM pulsara_v3.turns WHERE id=%s", (intent.turn_id,)).fetchone()["status"] == "RUNNING"
            if action == "delete":
                with pytest.raises(KeyError):
                    await service.get(task["id"])
            else:
                actual = await service.get(task["id"])
                assert actual["status"] == ("ACTIVE" if action=="resume" else "PAUSED")
                assert actual["permission_mode"] == VALUES["permission_mode"]
                assert facts["schedule_revisions"][task["id"]] == actual["revision"]
        finally:
            await service.aclose()
    asyncio.run(run())


def test_creation_defaults_and_permission_are_frozen_by_the_actual_accepting_owner(repository):
    from zoneinfo import ZoneInfo
    from pulsara_agent.primitives.permission import PermissionMode
    async def run():
        lease, intent, _, cut = open_turn(repository, text="create schedule\n")
        service, _ = service_for(repository, lease)
        try:
            arguments = {"action":"create", "values":{"name":" 原名称 ","prompt":"原文\r\n不变","schedule":{"kind":"interval","seconds":900}}}
            candidate = admit(repository, lease, intent, cut, "manage_schedule", arguments)
            before, _ = await service._read("scheduled_next_time")
            result = await produce(service, candidate, "manage_schedule", arguments)
            after, _ = await service._read("scheduled_next_time")
            await accept(repository, lease, candidate, result, "manage_schedule", arguments)
            value, facts = saved(repository, candidate)
            task = await service.get(value["target"]["task_id"])
            assert task["name"] == " 原名称 " and task["prompt"] == "原文\r\n不变"
            assert task["timezone"] == "Asia/Shanghai" and task["status"] == "PAUSED"
            assert task["permission_mode"] == DEFAULT_PERMISSION_MODE.value
            anchor = datetime.fromisoformat(task["schedule"]["anchor_at_utc"])
            assert before.replace(microsecond=0) <= anchor <= after
            assert facts == {"schedule_revisions":{task["id"]:1}}
            # A later turn's different authority cannot silently rewrite it.
            observed = await service.invoke_query({}, default_session_id=lease.guard.session_id,
                permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            assert observed.value.content[0].value["permission_mode"] == DEFAULT_PERMISSION_MODE.value
            daily = await service.create(session_id=lease.guard.session_id, values={
                "name":"daily","prompt":"p","permission_mode":"read-only","schedule":{"kind":"daily","time":"09:00"}})
            assert daily["schedule"]["start_date"] == after.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        finally:
            await service.aclose()
    asyncio.run(run())
