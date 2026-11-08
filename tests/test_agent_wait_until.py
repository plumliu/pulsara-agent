"""The until/settle matrix and its observable race boundaries."""

import asyncio

import pytest

from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import agent_target, work_target
from pulsara_agent.primitives.tool_result_budget import ToolResultPageBoundary
from tests import test_agent_collaboration_api as api_tests


@pytest.mark.parametrize("states,expected", [
    (("completed", "running"), ("settled", "timeout", "settled", "timeout")),
    (("running", "queued"), ("timeout", "unavailable", "timeout", "timeout")),
    (("completed", "queued"), ("settled", "unavailable", "settled", "timeout")),
    (("queued", "queued"), ("unavailable", "unavailable", "timeout", "timeout")),
    (("completed", "completed"), ("settled",) * 4),
    (("running", "running"), ("timeout",) * 4),
])
def test_first_observation_matrix(states, expected):
    async def scenario():
        for (until, settle), state in zip(
            (("work_end", "first"), ("work_end", "all"), ("idle", "first"), ("idle", "all")),
            expected, strict=True,
        ):
            h = api_tests.FacadeTests()
            h.setUp()
            targets = [agent_target(h.add_work(status=status)[0]) for status in states]
            result = await h.call("wait_agent", {
                "agents": targets, "until": until, "settle": settle, "timeout_seconds": 0,
            })
            assert result["state"] == state, (states, until, settle, result)
            assert result["until"] == until
            assert [item["target"] for item in result["observations"]] == targets
            assert h.runtime.suspensions == 0
            assert not {"bindings", "works"} & result.keys()
    asyncio.run(scenario())


async def entered_wait(h, count=1):
    async with asyncio.timeout(1):
        while h.runtime.waiters < count:
            await asyncio.sleep(0)


@pytest.mark.parametrize("until", ["work_end", "idle"])
def test_initial_idle_and_later_work_are_distinct_by_mode(until):
    async def scenario():
        h = api_tests.FacadeTests()
        h.setUp()
        a, _ = h.add_work(status="completed")
        b, w1 = h.add_work()
        waiter = asyncio.create_task(h.call("wait_agent", {
            "agents": [agent_target(a), agent_target(b)], "until": until,
        }))
        await entered_wait(h)
        _, w2 = h.add_work(agent=a)
        h.repo.rows[w1]["status"] = "completed"
        await h.runtime.changed()
        if until == "idle":
            await entered_wait(h, 2)
            assert not waiter.done()
            h.repo.rows[w2]["status"] = "completed"
            await h.runtime.changed()
        result = await asyncio.wait_for(waiter, 1)
        assert result["state"] == "settled"
        if until == "work_end":
            assert result["observations"] == [
                {"target": agent_target(a), "condition": "idle"},
                {"target": agent_target(b), "condition": "work_ended", "work": work_target(w1)},
            ]
            assert h.repo.rows[w2]["status"] == "running"
    asyncio.run(scenario())


def test_first_can_finish_a_bound_work_while_unbound_agent_starts_later():
    async def scenario():
        h = api_tests.FacadeTests()
        h.setUp()
        a, w1 = h.add_work()
        b, w2 = h.add_work(status="queued")
        waiter = asyncio.create_task(h.call("wait_agent", {
            "agents": [agent_target(a), agent_target(b)], "until": "work_end", "settle": "first",
        }))
        await entered_wait(h)
        h.repo.rows[w2]["status"] = "running"
        h.repo.rows[w1]["status"] = "stopped"
        await h.runtime.changed()
        result = await asyncio.wait_for(waiter, 1)
        assert result["state"] == "settled"
        assert result["observations"][1] == {"target": agent_target(b), "condition": "not_started"}
        assert result["pending_targets"] == [agent_target(b)]
        assert result["observations"][0]["work"] == work_target(w1)
    asyncio.run(scenario())


def test_idle_all_does_not_accumulate_idle_or_ignore_new_queue_items():
    async def scenario():
        h = api_tests.FacadeTests()
        h.setUp()
        a, w1 = h.add_work()
        b, w2 = h.add_work()
        waiter = asyncio.create_task(h.call("wait_agent", {"agents": [agent_target(a), agent_target(b)]}))
        await entered_wait(h)
        h.repo.rows[w1]["status"] = "completed"
        await h.runtime.changed()
        await entered_wait(h, 2)
        _, w3 = h.add_work(agent=a, status="queued")
        h.repo.rows[w2]["status"] = "completed"
        await h.runtime.changed()
        await entered_wait(h, 3)
        assert not waiter.done()
        h.repo.rows[w3]["status"] = "stopped"
        await h.runtime.changed()
        result = await asyncio.wait_for(waiter, 1)
        assert result["state"] == "settled"
        assert result["pending_targets"] == []
        assert all(item["condition"] == "idle" and "work" not in item for item in result["observations"])
    asyncio.run(scenario())


def test_already_satisfied_sender_still_wakes_work_end_all():
    async def scenario():
        h = api_tests.FacadeTests()
        h.setUp()
        a, _ = h.add_work(status="completed")
        b, _ = h.add_work()
        waiter = asyncio.create_task(h.call("wait_agent", {
            "agents": [agent_target(a), agent_target(b)], "until": "work_end",
        }))
        await entered_wait(h)
        h.runtime.messages[a] = 1
        await h.runtime.changed()
        result = await asyncio.wait_for(waiter, 1)
        assert result["state"] == "message_available"
        assert result["message_sources"] == [agent_target(a)]
        assert result["pending_targets"] == [agent_target(b)]
    asyncio.run(scenario())


def test_message_precedes_settled_but_control_precedes_message_and_cursor_is_not_consumed():
    async def scenario():
        h = api_tests.FacadeTests()
        h.setUp()
        a, _ = h.add_work(status="completed")
        h.runtime.messages[a] = 1
        arguments = {"agents": [agent_target(a)], "timeout_seconds": 0}
        h.context.cell_cancel_event.set()
        assert (await h.call("wait_agent", arguments))["state"] == "interrupted"
        assert h.context.agent_message_cursors == {}
        h.context.cell_cancel_event.clear()
        result = await h.call("wait_agent", arguments)
        assert result["state"] == "message_available" and result["pending_targets"] == []
        assert (await h.call("wait_agent", arguments))["state"] == "settled"
    asyncio.run(scenario())


def test_budget_rejection_does_not_advance_message_cursor():
    from dataclasses import replace
    async def scenario():
        h = api_tests.FacadeTests()
        h.setUp()
        a, _ = h.add_work()
        h.runtime.messages[a] = 1
        h.context.page_budget = replace(h.context.page_budget, accepts_provider_body=lambda *_: False)
        with pytest.raises(ToolResultPageBoundary):
            await h.call("wait_agent", {"agents": [agent_target(a)]})
        assert h.context.agent_message_cursors == {}
        assert h.runtime.suspensions == 0
    asyncio.run(scenario())


def test_change_between_initial_read_and_suspend_is_not_lost():
    async def scenario():
        h = api_tests.FacadeTests()
        h.setUp()
        a, w1 = h.add_work()
        original = h.api._agent_wait_snapshot
        reads = 0
        async def snapshot(*args):
            nonlocal reads
            rows = await original(*args)
            reads += 1
            if reads == 1:
                h.repo.rows[w1]["status"] = "completed"
                await h.runtime.changed()
            return rows
        h.api._agent_wait_snapshot = snapshot
        result = await asyncio.wait_for(h.call("wait_agent", {"agents": [agent_target(a)]}), 1)
        assert result["state"] == "settled" and reads == 2
    asyncio.run(scenario())


def test_disappearing_target_is_not_idle():
    async def scenario():
        h = api_tests.FacadeTests()
        h.setUp()
        a, _ = h.add_work()
        waiter = asyncio.create_task(h.call("wait_agent", {"agents": [agent_target(a)]}))
        await entered_wait(h)
        h.repo.agents.remove(a)
        await h.runtime.changed()
        with pytest.raises(ValueError, match="does not exist"):
            await asyncio.wait_for(waiter, 1)
    asyncio.run(scenario())
