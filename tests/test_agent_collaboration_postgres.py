"""Public API against canonical PostgreSQL session/request/turn owners."""

import asyncio
from functools import wraps
from types import SimpleNamespace

import pytest
from psycopg.rows import dict_row

from pulsara_agent.identity import random_id
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.repository import ConversationKernelConflict
from pulsara_agent.conversation_kernel.subagents.collaboration_api import (
    CollaborationToolPort,
)
from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import (
    prepare_batch_refs,
    work_target,
)
from pulsara_agent.llm.model_connections import model_call_binding_from_dict
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.test_agent_requests_persistence import start, finish, request, deadline, now
from tests.test_agent_collaboration_api import RuntimeOwner

pytest_plugins = ["tests.test_agent_requests_persistence"]
pytestmark = pytest.mark.postgres


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


@pytest.fixture
def api_source(source):
    repository, root, admit = source
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=deadline(),
    ) as connection:
        turn = connection.execute(
            "SELECT * FROM pulsara_v3.turns WHERE session_id=%s AND status='RUNNING'",
            (root.guard.session_id,),
        ).fetchone()
        attempt = connection.execute(
            "SELECT a.id FROM pulsara_v3.tool_execution_attempts a JOIN pulsara_v3.tool_invocations i ON i.id=a.invocation_id WHERE a.session_id=%s AND i.turn_id=%s",
            (root.guard.session_id, turn["id"]),
        ).fetchone()
        permission = repository._permission_from_row(turn)
    snapshot = SimpleNamespace(
        model_call_binding=model_call_binding_from_dict(turn["model_call_binding"]),
        permission_snapshot=permission,
        initial_context_units=("ROOT accepted context",),
    )
    context = SimpleNamespace(
        session_id=root.guard.session_id,
        turn_id=turn["id"],
        attempt_id=attempt["id"],
        effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
        invocation_source=SimpleNamespace(kind="CODE_MODE_CALL"),
        agent_message_cursors={},
        steer_event=asyncio.Event(),
        agent_invocation_snapshot=snapshot,
    )

    class Observations(RuntimeOwner):
        def read_deadline(self):
            return deadline()

        async def pending_message_sources(
            self,
            *,
            recipient_session_id,
            source_agent_ids,
            after_by_sender,
            invocation_context,
        ):
            return await asyncio.to_thread(
                repository.pending_agent_message_sources,
                session_id=recipient_session_id,
                turn_id=invocation_context.turn_id,
                source_session_ids=source_agent_ids,
                after_by_sender=after_by_sender,
                deadline_monotonic=deadline(),
            )

    runtime = Observations(repository)
    api = CollaborationToolPort(
        repository=repository,
        root_session_id=root.guard.session_id,
        session_id=root.guard.session_id,
        runtime=runtime,
        io=KernelSessionIO(),
        admission_facts=lambda context, requests: dict(
            root_guard=root.guard, actor_id="api-test", deadline_monotonic=deadline()
        ),
    )
    return repository, root, api, runtime, context


async def call(api, context, name, args):
    return (
        await api.invoke(tool_name=name, arguments=args, invocation_context=context)
    ).public_value()


@async_test
async def test_public_batch_atomic_rejection_and_wait_reads_exact_real_finals(
    api_source,
):
    repository, root, api, runtime, context = api_source
    peers = prepare_batch_refs({"a": {}, "b": {}})
    bad = {
        "members": [
            dict(
                key=key,
                agent_id=ref["agent"]["agent_id"],
                work_id=ref["work"]["work_id"],
                parameters={
                    "instruction": key,
                    **(
                        {"materials": [work_target("queue:aaaaaaaaaaaaaaaaaaaaaaaaaa")]}
                        if key == "b"
                        else {}
                    ),
                },
            )
            for key, ref in peers.items()
        ]
    }
    with pytest.raises(
        ConversationKernelConflict, match="material Work must have ended"
    ):
        await call(api, context, "run_agent.batch", bad)
    assert (
        repository.list_root_agent_session_ids(
            root_session_id=root.guard.session_id, deadline_monotonic=deadline()
        )
        == ()
    )
    bad["members"][1]["parameters"] = {
        "instruction": "compare",
        "depends_on": [peers["a"]["work"]],
    }
    accepted = await call(api, context, "run_agent.batch", bad)
    assert accepted["a"]["work"] == peers["a"]["work"]
    waiting = asyncio.create_task(
        call(
            api,
            context,
            "wait_agent.works",
            {"targets": [peers["a"]["work"], peers["b"]["work"]]},
        )
    )
    while not runtime.waiters:
        await asyncio.sleep(0)
    a = request(
        agent_id=peers["a"]["agent"]["agent_id"], work_id=peers["a"]["work"]["work_id"]
    )
    b = request(
        agent_id=peers["b"]["agent"]["agent_id"], work_id=peers["b"]["work"]["work_id"]
    )
    lease_a, candidate_a = start(repository, a)
    final_a = "full-final:" + "x" * 100_000
    finish(repository, lease_a, candidate_a, final_a)
    repository.resolve_agent_dependencies(
        session_id=b.agent_id, deadline_monotonic=deadline()
    )
    await runtime.changed()
    await asyncio.sleep(0)
    assert not waiting.done()
    lease_b, candidate_b = start(repository, b)
    finish(repository, lease_b, candidate_b, "comparison final")
    await runtime.changed()
    result = await asyncio.wait_for(waiting, 3)
    assert result["state"] == "settled"
    assert [work["status"] for work in result["works"]] == ["completed", "completed"]
    assert result["works"][0]["final"] == final_a
    assert result["works"][1]["final"] == "comparison final"
    assert result["pending_targets"] == []


@async_test
async def test_public_stop_cascades_queued_dependencies_and_preserves_history(
    api_source,
):
    repository, root, api, runtime, context = api_source
    a = await call(api, context, "run_agent", {"instruction": "first"})
    b = await call(
        api,
        context,
        "run_agent",
        {"instruction": "dependent", "depends_on": [a["work"]]},
    )
    independent = await call(
        api,
        context,
        "run_agent",
        {"target": b["agent"], "instruction": "independent next"},
    )
    stopped = await call(api, context, "stop_agent", {"target": a["work"]})
    assert stopped["works"][0]["status"] == "stopped"
    dependent = await call(api, context, "query_agents", {"target": b["work"]})
    assert dependent["status"] == "stopped"
    assert dependent["failed_dependency"] == a["work"]
    assert (await call(api, context, "query_agents", {"target": independent["work"]}))[
        "status"
    ] == "queued"
    rows = await call(api, context, "query_agents", {"target": b["agent"]})
    assert [work["target"] for work in rows["works"]] == [
        b["work"],
        independent["work"],
    ]
    outcome = await call(api, context, "stop_agent", {"target": b["agent"]})
    assert [work["target"] for work in outcome["works"]] == [independent["work"]]


@async_test
async def test_public_agent_wait_observes_real_inbox_without_copying_message_body(
    api_source,
):
    repository, root, api, runtime, context = api_source
    a = await call(api, context, "run_agent", {"instruction": "send a question"})
    work = request(agent_id=a["agent"]["agent_id"], work_id=a["work"]["work_id"])
    child, candidate = start(repository, work)
    queued = repository.enqueue_agent_steer(
        root.guard,
        target_turn_id=context.turn_id,
        queue_item_id=random_id("queue"),
        source_session_id=work.agent_id,
        source_work_id=work.work_id,
        text="actual question body",
        occurred_at=now(),
        actor_id="api-test",
        deadline_monotonic=deadline(),
    )
    assert queued is not None
    result = await call(
        api, context, "wait_agent", {"targets": [a["agent"]], "timeout_seconds": 0}
    )
    assert result["state"] == "message_available"
    assert result["message_sources"] == [a["agent"]]
    assert "actual question body" not in str(result)
    assert result["bindings"][0]["work"] == a["work"]


@async_test
async def test_public_admission_confirms_a_real_commit_after_acknowledgement_loss(
    api_source, monkeypatch
):
    repository, root, api, runtime, context = api_source
    original = repository.admit_agent_requests
    submissions = []

    def commit_then_lose_ack(*args, **kwargs):
        submissions.append(tuple(r.work_id for r in kwargs["requests"]))
        original(*args, **kwargs)
        raise ConnectionError("test: commit acknowledgement lost")

    monkeypatch.setattr(repository, "admit_agent_requests", commit_then_lose_ack)
    accepted = await call(
        api,
        context,
        "run_agent",
        {"instruction": "confirm this exact accepted request"},
    )
    assert submissions == [(accepted["work"]["work_id"],)]
    actual = repository.query_agent_work(
        root_session_id=root.guard.session_id,
        work_id=accepted["work"]["work_id"],
        deadline_monotonic=deadline(),
    )
    assert actual["instruction"] == "confirm this exact accepted request"
    assert runtime.wakes == 1
