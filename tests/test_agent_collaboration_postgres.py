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
    collaboration_output_schema,
)
from pulsara_agent.conversation_kernel.subagents.names import SCIENTIST_NAMES
from pulsara_agent.llm.model_connections import model_call_binding_from_dict
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.test_agent_requests_persistence import (
    start,
    finish,
    request,
    deadline,
    now,
    source as source,
)
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


def records(value):
    return [part["value"] for part in value["content"] if part["type"] == "object"]


def work_targets(value):
    return [row["work"] for row in records(value)]


def agent_targets(value):
    return [row["agent"] for row in records(value)]


async def call(api, context, name, args):
    value = (
        await api.invoke(tool_name=name, arguments=args, invocation_context=context)
    ).public_value()
    from jsonschema import Draft202012Validator
    Draft202012Validator(collaboration_output_schema(name)).validate(value)
    return value


@async_test
async def test_public_batch_atomic_rejection_and_wait_then_query_exact_real_finals(
    api_source,
):
    repository, root, api, runtime, context = api_source
    peers, bad = envelope(
        [
            {"instruction": "a"},
            {
                "instruction": "b",
                "materials": [work_target("queue:aaaaaaaaaaaaaaaaaaaaaaaaaa")],
            },
        ]
    )
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
    assert work_targets(accepted)[0] == peers["works"][0]
    waiting = asyncio.create_task(
        call(
            api,
            context,
            "wait_agent",
            {"agents": peers["agents"]},
        )
    )
    while not runtime.waiters:
        if waiting.done():
            await waiting
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
    assert result["meta"]["wait_reason"] == "condition_met"
    assert [row["is_idle"] for row in records(result)] == [True, True]
    assert all(
        not {"final", "diagnostic", "instruction"}.intersection(work)
        for work in records(result)
    )
    # Exact Work targets remain usable from a subsequent CODE caller context;
    # the wait value carries status, while explicit query reads full content.
    subsequent = SimpleNamespace(**vars(context))
    subsequent.agent_message_cursors = {}
    subsequent.cell_cancel_event = asyncio.Event()
    selected_a = await call(
        api, subsequent, "query_agents", {"target": peers["works"][0]}
    )
    selected_b = await call(
        api, subsequent, "query_agents", {"target": peers["works"][1]}
    )
    selected_a = records(selected_a)[0]
    assert selected_a["target"] == peers["works"][0]
    assert selected_a["agent"] == peers["agents"][0]
    assert selected_a["final"] == final_a
    selected_b = records(selected_b)[0]
    assert selected_b["target"] == peers["works"][1]
    assert selected_b["agent"] == peers["agents"][1]
    assert selected_b["final"] == "comparison final"


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
        envelope([{"instruction": "dependent", "depends_on": [work_targets(a)[0]]}])[1],
    )
    independent = await call(
        api,
        context,
        "run_agent",
        envelope([{"target": agent_targets(b)[0], "instruction": "independent next"}])[
            1
        ],
    )
    stopped = await call(api, context, "stop_agent", {"target": work_targets(a)[0]})
    assert records(stopped)[0]["work_status"] == "stopped"
    dependent = await call(api, context, "query_agents", {"target": work_targets(b)[0]})
    dependent = records(dependent)[0]
    assert dependent["work_status"] == "stopped"
    assert dependent["failed_dependency"] == work_targets(a)[0]
    assert (
        records(
            await call(
                api, context, "query_agents", {"target": work_targets(independent)[0]}
            )
        )[0]["work_status"]
        == "queued"
    )
    rows = await call(api, context, "query_agents", {"target": agent_targets(b)[0]})
    assert [work["target"] for work in records(rows)] == [
        work_targets(b)[0],
        work_targets(independent)[0],
    ]
    outcome = await call(api, context, "stop_agent", {"target": agent_targets(b)[0]})
    assert [work["target"] for work in records(outcome)] == [
        work_targets(independent)[0]
    ]


@async_test
async def test_public_agent_wait_observes_real_inbox_without_copying_message_body(
    api_source,
):
    repository, root, api, runtime, context = api_source
    a = await call(
        api, context, "run_agent", envelope([{"instruction": "send a question"}])[1]
    )
    work = request(
        agent_id=agent_targets(a)[0]["agent_id"], work_id=work_targets(a)[0]["work_id"]
    )
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
        api,
        context,
        "wait_agent",
        {"agents": [agent_targets(a)[0]], "timeout_seconds": 0},
    )
    assert result["meta"]["wait_reason"] == "message_available"
    assert result["meta"]["message_sources"] == [agent_targets(a)[0]]
    assert "actual question body" not in str(result)
    assert records(result) == [{"agent": agent_targets(a)[0], "is_idle": False}]


@async_test
async def test_wait_snapshot_reads_current_queue_and_exact_bound_work_together(
    api_source,
):
    repository, root, api, runtime, context = api_source
    result = await call(
        api,
        context,
        "run_agent",
        envelope(
            [
                {"instruction": "current"},
                {"instruction": "still queued"},
            ]
        )[1],
    )
    a, b = [item["agent_id"] for item in agent_targets(result)]
    w1, w2 = [item["work_id"] for item in work_targets(result)]
    child, candidate = start(repository, request(agent_id=a, work_id=w1))

    def snapshot():
        return repository.read_agent_wait_states(
            root_session_id=root.guard.session_id,
            agent_ids=(a, b),
            bound_work_ids=(w1, None),
            deadline_monotonic=deadline(),
        )

    rows = snapshot()
    assert rows[0] == dict(
        agent_id=a,
        current_work_id=w1,
        has_running_turn=True,
        has_pending_work=False,
        bound_work_id=w1,
        bound_work_status="running",
    )
    assert rows[1] == dict(
        agent_id=b,
        current_work_id=None,
        has_running_turn=False,
        has_pending_work=True,
        bound_work_id=None,
        bound_work_status=None,
    )
    finish(repository, child, candidate, "current complete")
    followup = await call(
        api,
        context,
        "run_agent",
        envelope(
            [
                {"target": agent_targets(result)[0], "instruction": "followup"},
            ]
        )[1],
    )
    rows = snapshot()
    assert rows[0]["bound_work_status"] == "completed"
    assert rows[0]["current_work_id"] is None and rows[0]["has_pending_work"]
    next_child, next_candidate = start(
        repository, request(agent_id=a, work_id=work_targets(followup)[0]["work_id"])
    )
    rows = snapshot()
    assert rows[0]["current_work_id"] == work_targets(followup)[0]["work_id"]
    assert (
        rows[0]["bound_work_id"] == w1 and rows[0]["bound_work_status"] == "completed"
    )
    assert rows[1]["has_pending_work"]


@async_test
async def test_same_agent_batch_waits_once_for_idle_and_queries_each_work(api_source):
    repository, root, api, runtime, context = api_source
    first = await call(
        api, context, "run_agent", envelope([{"instruction": "initial"}])[1]
    )
    a = agent_targets(first)[0]
    child, candidate = start(
        repository,
        request(agent_id=a["agent_id"], work_id=work_targets(first)[0]["work_id"]),
    )
    finish(repository, child, candidate, "initial result")
    batch = await call(
        api,
        context,
        "run_agent",
        envelope(
            [
                {"target": a, "instruction": "second"},
                {"target": a, "instruction": "third"},
            ]
        )[1],
    )
    assert agent_targets(batch) == [a, a]
    agents = list(
        {target["agent_id"]: target for target in agent_targets(batch)}.values()
    )
    waiting = asyncio.create_task(call(api, context, "wait_agent", {"agents": agents}))
    while not runtime.waiters:
        if waiting.done():
            await waiting
        await asyncio.sleep(0)
    for index, target in enumerate(work_targets(batch)):
        lease, candidate = start(
            repository, request(agent_id=a["agent_id"], work_id=target["work_id"])
        )
        finish(repository, lease, candidate, f"result {index}")
        await runtime.changed()
        if index == 0:
            while runtime.waiters < 2:
                await asyncio.sleep(0)
            assert not waiting.done()
    result = await asyncio.wait_for(waiting, 3)
    assert result["meta"]["wait_reason"] == "condition_met"
    assert records(result) == [{"agent": a, "is_idle": True}]
    actual = [
        await call(api, context, "query_agents", {"target": target})
        for target in work_targets(batch)
    ]
    assert [records(work)[0]["final"] for work in actual] == ["result 0", "result 1"]


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
    assert submissions == [(work_targets(accepted)[0]["work_id"],)]
    actual = repository.query_agent_work(
        root_session_id=root.guard.session_id,
        work_id=work_targets(accepted)[0]["work_id"],
        deadline_monotonic=deadline(),
    )
    assert actual["instruction"] == "confirm this exact accepted request"
    assert runtime.wakes == 1


@async_test
async def test_real_work_titles_belong_to_each_admission_not_agent_session(api_source):
    repository, root, api, runtime, context = api_source
    _, args = envelope([{"instruction": "first exact body", "work_title": "第一项"}])
    first = await call(api, context, "run_agent", args)
    initial_agents = await call(api, context, "query_agents", {})
    agent_name = records(initial_agents)[0]["name"]
    assert agent_name in SCIENTIST_NAMES
    _, args = envelope(
        [
            {
                "target": agent_targets(first)[0],
                "instruction": "second exact body",
                "work_title": "同名",
            },
            {
                "target": agent_targets(first)[0],
                "instruction": "third exact body",
                "work_title": "同名",
            },
            {"target": agent_targets(first)[0], "instruction": "fourth exact body"},
        ]
    )
    next_result = await call(api, context, "run_agent", args)
    assert agent_targets(next_result) == agent_targets(first) * 3
    assert (
        len(
            {
                item["work_id"]
                for item in work_targets(first) + work_targets(next_result)
            }
        )
        == 4
    )
    targets = work_targets(first) + work_targets(next_result)
    for target, title, instruction in zip(
        targets,
        ["第一项", "同名", "同名", None],
        [
            "first exact body",
            "second exact body",
            "third exact body",
            "fourth exact body",
        ],
        strict=True,
    ):
        fact = await call(api, context, "query_agents", {"target": target})
        fact = records(fact)[0]
        assert fact["work_title"] == title and fact["instruction"] == instruction
    listed = await call(
        api, context, "query_agents", {"target": agent_targets(first)[0]}
    )
    assert [item["target"] for item in records(listed)] == targets
    assert [item["work_title"] for item in records(listed)] == [
        "第一项",
        "同名",
        "同名",
        None,
    ]
    await call(api, context, "stop_agent", {"target": targets[-1]})
    stopped = await call(api, context, "query_agents", {"target": targets[-1]})
    stopped = records(stopped)[0]
    assert stopped["work_title"] is None and stopped["work_status"] == "stopped"
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=deadline(),
    ) as connection:
        row = connection.execute(
            "SELECT title FROM pulsara_v3.sessions WHERE id=%s",
            (agent_targets(first)[0]["agent_id"],),
        ).fetchone()
    assert row["title"] == agent_name


@async_test
async def test_new_agents_receive_distinct_saved_names_across_batches(api_source):
    repository, root, api, runtime, context = api_source
    targets = []
    for _ in range(2):
        _, args = envelope([{"instruction": "first"}, {"instruction": "second"}])
        result = await call(api, context, "run_agent", args)
        targets.extend(agent_targets(result))
    listed = await call(api, context, "query_agents", {})
    names = {item["target"]["agent_id"]: item["name"] for item in records(listed)}
    assert set(names) == {target["agent_id"] for target in targets}
    assert len(set(names.values())) == 4
    assert set(names.values()) <= set(SCIENTIST_NAMES)
    for agent_id, name in names.items():
        row = repository.read_agent_runtime_state(
            session_id=agent_id, deadline_monotonic=deadline()
        )
        assert row["title"] == name
