"""Public API against canonical PostgreSQL session/request/turn owners."""

import asyncio
from functools import wraps
from types import SimpleNamespace

import pytest
from psycopg.rows import dict_row

from pulsara_agent.identity import random_id
from tests.test_agent_collaboration_array_contracts import envelope
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.repository import ConversationKernelConflict
from pulsara_agent.conversation_kernel.subagents.collaboration_api import (
    CollaborationToolPort,
)
from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import (
    work_target,
)
from pulsara_agent.conversation_kernel.subagents.names import SCIENTIST_NAMES
from pulsara_agent.llm.model_connections import model_call_binding_from_dict
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.tool_result_budget import ToolResultPageBudget
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.test_agent_requests_persistence import start, finish, request, deadline, now, source as source
from tests.test_agent_collaboration_api import RuntimeOwner

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
        page_budget=ToolResultPageBudget(
            tool_call_id=random_id("call"), result_target={"kind": "SESSION_ENTRY", "session_id": root.guard.session_id,
                "entry_id": random_id("entry")}),
        session_id=root.guard.session_id,
        turn_id=turn["id"],
        attempt_id=attempt["id"],
        effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
        invocation_source=SimpleNamespace(kind="CODE_MODE_CALL"),
        agent_message_cursors={},
        cell_cancel_event=asyncio.Event(),
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
async def test_public_batch_atomic_rejection_and_wait_then_query_exact_real_finals(
    api_source,
):
    repository, root, api, runtime, context = api_source
    peers, bad = envelope([
        {"instruction": "a"},
        {"instruction": "b", "materials": [work_target("queue:aaaaaaaaaaaaaaaaaaaaaaaaaa")]},
    ])
    with pytest.raises(
        ConversationKernelConflict, match="material Work must have ended"
    ):
        await call(api, context, "run_agent", bad)
    assert (
        repository.list_root_agent_session_ids(
            root_session_id=root.guard.session_id, deadline_monotonic=deadline()
        )
        == ()
    )
    bad["members"][1]["parameters"] = {
        "instruction": "compare",
        "depends_on": [peers["works"][0]],
    }
    accepted = await call(api, context, "run_agent", bad)
    assert accepted["works"][0] == peers["works"][0]
    waiting = asyncio.create_task(
        call(
            api,
            context,
            "wait_agent",
            {"works": [peers["works"][0], peers["works"][1]]},
        )
    )
    while not runtime.waiters:
        await asyncio.sleep(0)
    a = request(
        agent_id=peers["agents"][0]["agent_id"], work_id=peers["works"][0]["work_id"]
    )
    b = request(
        agent_id=peers["agents"][1]["agent_id"], work_id=peers["works"][1]["work_id"]
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
    assert all(not {"final", "diagnostic", "instruction"}.intersection(work)
               for work in result["works"])
    # Exact Work targets remain usable from a subsequent CODE caller context;
    # the wait value carries status, while explicit query reads full content.
    subsequent = SimpleNamespace(**vars(context))
    subsequent.agent_message_cursors = {}
    subsequent.cell_cancel_event = asyncio.Event()
    selected_a = await call(api, subsequent, "query_agents", {"target": result["works"][0]["target"]})
    selected_b = await call(api, subsequent, "query_agents", {"target": result["works"][1]["target"]})
    assert selected_a["target"] == peers["works"][0]
    assert selected_a["agent"] == peers["agents"][0]
    assert selected_a["final"] == final_a
    assert selected_b["target"] == peers["works"][1]
    assert selected_b["agent"] == peers["agents"][1]
    assert selected_b["final"] == "comparison final"
    assert result["pending_targets"] == []


@async_test
async def test_public_stop_cascades_queued_dependencies_and_preserves_history(
    api_source,
):
    repository, root, api, runtime, context = api_source
    a = await call(api, context, "run_agent", envelope([{"instruction": "first"}])[1])
    b = await call(
        api,
        context,
        "run_agent",
        envelope([{"instruction": "dependent", "depends_on": [a["works"][0]]}])[1],
    )
    independent = await call(
        api,
        context,
        "run_agent",
        envelope([{"target": b["agents"][0], "instruction": "independent next"}])[1],
    )
    stopped = await call(api, context, "stop_agent", {"target": a["works"][0]})
    assert stopped["works"][0]["status"] == "stopped"
    dependent = await call(api, context, "query_agents", {"target": b["works"][0]})
    assert dependent["status"] == "stopped"
    assert dependent["failed_dependency"] == a["works"][0]
    assert (await call(api, context, "query_agents", {"target": independent["works"][0]}))[
        "status"
    ] == "queued"
    rows = await call(api, context, "query_agents", {"target": b["agents"][0]})
    assert [work["target"] for work in rows["works"]] == [
        b["works"][0],
        independent["works"][0],
    ]
    outcome = await call(api, context, "stop_agent", {"target": b["agents"][0]})
    assert [work["target"] for work in outcome["works"]] == [independent["works"][0]]


@async_test
async def test_public_agent_wait_observes_real_inbox_without_copying_message_body(
    api_source,
):
    repository, root, api, runtime, context = api_source
    a = await call(api, context, "run_agent", envelope([{"instruction": "send a question"}])[1])
    work = request(agent_id=a["agents"][0]["agent_id"], work_id=a["works"][0]["work_id"])
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
        api, context, "wait_agent", {"agents": [a["agents"][0]], "timeout_seconds": 0}
    )
    assert result["state"] == "message_available"
    assert result["message_sources"] == [a["agents"][0]]
    assert "actual question body" not in str(result)
    assert result["bindings"][0]["work"] == a["works"][0]


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
        envelope([{"instruction": "confirm this exact accepted request"}])[1],
    )
    assert submissions == [(accepted["works"][0]["work_id"],)]
    actual = repository.query_agent_work(
        root_session_id=root.guard.session_id,
        work_id=accepted["works"][0]["work_id"],
        deadline_monotonic=deadline(),
    )
    assert actual["instruction"] == "confirm this exact accepted request"
    assert runtime.wakes == 1


@async_test
async def test_real_work_titles_belong_to_each_admission_not_agent_session(api_source):
    repository, root, api, runtime, context = api_source
    _, args = envelope([{'instruction': 'first exact body', 'work_title': '第一项'}])
    first = await call(api, context, 'run_agent', args)
    initial_agents = await call(api, context, 'query_agents', {})
    agent_name = initial_agents['agents'][0]['name']
    assert agent_name in SCIENTIST_NAMES
    _, args = envelope([
        {'target': first['agents'][0], 'instruction': 'second exact body', 'work_title': '同名'},
        {'target': first['agents'][0], 'instruction': 'third exact body', 'work_title': '同名'},
        {'target': first['agents'][0], 'instruction': 'fourth exact body'},
    ])
    next_result = await call(api, context, 'run_agent', args)
    assert next_result['agents'] == first['agents'] * 3
    assert len({item['work_id'] for item in first['works'] + next_result['works']}) == 4
    assert [item['work_title'] for item in next_result['details']] == ['同名', '同名', None]
    targets = first['works'] + next_result['works']
    for target, title, instruction in zip(targets, ['第一项', '同名', '同名', None],
            ['first exact body', 'second exact body', 'third exact body', 'fourth exact body'], strict=True):
        fact = await call(api, context, 'query_agents', {'target': target})
        assert fact['work_title'] == title and fact['instruction'] == instruction
    listed = await call(api, context, 'query_agents', {'target': first['agents'][0]})
    assert [item['target'] for item in listed['works']] == targets
    assert [item['work_title'] for item in listed['works']] == ['第一项', '同名', '同名', None]
    await call(api, context, 'stop_agent', {'target': targets[-1]})
    stopped = await call(api, context, 'query_agents', {'target': targets[-1]})
    assert stopped['work_title'] is None and stopped['status'] == 'stopped'
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row, deadline_monotonic=deadline()
    ) as connection:
        row = connection.execute('SELECT title FROM pulsara_v3.sessions WHERE id=%s',
            (first['agents'][0]['agent_id'],)).fetchone()
    assert row['title'] == agent_name


@async_test
async def test_new_agents_receive_distinct_saved_names_across_batches(api_source):
    repository, root, api, runtime, context = api_source
    targets = []
    for _ in range(2):
        _, args = envelope([{'instruction': 'first'}, {'instruction': 'second'}])
        result = await call(api, context, 'run_agent', args)
        targets.extend(result['agents'])
    listed = await call(api, context, 'query_agents', {})
    names = {item['agent']['agent_id']: item['name'] for item in listed['agents']}
    assert set(names) == {target['agent_id'] for target in targets}
    assert len(set(names.values())) == 4
    assert set(names.values()) <= set(SCIENTIST_NAMES)
    for agent_id, name in names.items():
        row = repository.read_agent_runtime_state(session_id=agent_id, deadline_monotonic=deadline())
        assert row['title'] == name
