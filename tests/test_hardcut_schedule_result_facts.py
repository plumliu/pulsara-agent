"""Actual schedule rows freeze public observations with matching internal revisions."""
import asyncio
from datetime import datetime, timezone
from time import monotonic
from types import SimpleNamespace as NS

import pytest

from pulsara_agent.conversation_kernel.repository import AssistantToolCallBlock, ConversationKernelRepository
from pulsara_agent.conversation_kernel.tool_outputs import PostgresToolOutputReadPort, project_tool_result_export
from pulsara_agent.conversation_kernel.tool_runtime_management import _ToolManagementOperations
from pulsara_agent.identity import random_id
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.ports.tool_execution import thaw_tool_json_object
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from pulsara_agent.primitives.tool_result_budget import ToolResultPageBudget
from pulsara_agent.scheduling.requests import schedule_target
from pulsara_agent.scheduling.service import ScheduledTaskService
from tests.support.model_config import test_model_resolution_snapshot as model_resolution_snapshot
from tests.support.postgres import verified_postgres_provider
from tests.test_hardcut_entry_parts import open_turn, read
from tests.test_hardcut_tool_settlement import executor

pytestmark = pytest.mark.postgres
VALUES = {"name": "Saved work", "prompt": "Keep this original task", "timezone": "Asia/Shanghai",
    "permission_mode": "read-only", "schedule": {"kind": "interval", "anchor_at_utc": "2020-01-01T00:00:00+00:00", "seconds": 60}}


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
    assistant, call, attempt = random_id("entry"), "actual-schedule-call", random_id("attempt")
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
        page_budget=ToolResultPageBudget(tool_call_id=candidate.tool_call_id),
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
            second = await service.create(session_id=lease.guard.session_id, values={**VALUES, "schedule": {**VALUES["schedule"], "seconds": 120}})
            arguments = {"target": schedule_target(first["id"])} if detail else {}
            candidate = admit(repository, lease, intent, cut, "query_schedules", arguments)
            reads.clear()
            result = await produce(service, candidate, "query_schedules", arguments)
            assert [method for method, _ in reads] == (["read_scheduled_task"] if detail else ["list_scheduled_tasks"])
            expected = {first["id"]: first["revision"]}
            if not detail:
                expected[second["id"]] = second["revision"]
            assert result.result_facts == {"schedule_revisions": expected}
            # Later live configuration must not replace the query's original
            # config/revision pair while this known result is accepted.
            await service.mutate(task_id=first["id"], expected_revision=first["revision"],
                action="update", values={**VALUES, "schedule": {**VALUES["schedule"], "seconds": 300}})
            read_count = len(reads)
            await accept(repository, lease, candidate, result, "query_schedules", arguments)
            value, facts = saved(repository, candidate)
            assert facts == {"schedule_revisions": expected}
            public = value if detail else next(item for item in value["tasks"] if item["target"] == schedule_target(first["id"]))
            assert public["schedule"]["seconds"] == 60
            assert public["revision"] == first["revision"]  # E still owns this old public field.
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
                values={**VALUES, "schedule": {**VALUES["schedule"], "seconds": 120}})
            if switch:
                arguments = {"action": "switch", "target": schedule_target(source["id"]), "expected_revision": source["revision"],
                    "replacement": ({"kind": "existing", "target": schedule_target(destination["id"]), "expected_revision": destination["revision"]}
                        if switch == "existing" else {"kind": "create", "values": {**VALUES, "schedule": {**VALUES["schedule"], "seconds": 120}}})}
            else:
                arguments = {"action": "update", "target": schedule_target(source["id"]), "expected_revision": source["revision"],
                    "values": {**VALUES, "schedule": {**VALUES["schedule"], "seconds": 300}}}
            candidate = admit(repository, lease, intent, cut, "manage_schedule", arguments)
            result = await produce(service, candidate, "manage_schedule", arguments)
            expected = {source["id"]: source["revision"]+1}
            if switch:
                active_id = result.content.public_fields["active"]["target"]["task_id"]
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
                assert value["paused"]["status"] == "PAUSED" and value["active"]["status"] == "ACTIVE"
                assert value["paused"]["revision"] == expected[source["id"]]
                assert value["active"]["revision"] == expected[active_id]
                assert value["active"]["schedule"]["seconds"] == 120
            else:
                assert value["status"] == "ACTIVE" and value["revision"] == expected[source["id"]]
                assert value["schedule"]["seconds"] == 300
            assert len(reads) == read_count
        finally:
            await service.aclose()
    asyncio.run(run())
