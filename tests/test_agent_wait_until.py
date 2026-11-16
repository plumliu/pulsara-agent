"""Exact Work completion and Agent idle/message snapshots at race boundaries."""

import asyncio

import pytest

from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import (
    agent_target,
    work_target,
)
from tests import test_agent_collaboration_api as api_tests


def harness():
    value = api_tests.FacadeTests()
    value.setUp()
    return value


def records(result):
    return [part["value"] for part in result["content"] if part["type"] == "object"]


@pytest.mark.parametrize(
    "states,expected",
    [
        (("completed", "running"), ("condition_met", "timeout")),
        (("running", "queued"), ("timeout", "timeout")),
        (("completed", "queued"), ("condition_met", "timeout")),
        (("queued", "queued"), ("timeout", "timeout")),
        (("completed", "completed"), ("condition_met", "condition_met")),
        (("running", "running"), ("timeout", "timeout")),
        (("stopped", "completed"), ("condition_met", "condition_met")),
    ],
)
def test_first_observation_matrix(states, expected):
    async def scenario():
        for branch in ("works", "agents"):
            for settle, reason in zip(("first", "all"), expected, strict=True):
                h = harness()
                pairs = [h.add_work(status=status) for status in states]
                targets = [
                    work_target(w) if branch == "works" else agent_target(a)
                    for a, w in pairs
                ]
                result = await h.call(
                    "wait_agent",
                    {branch: targets, "settle": settle, "timeout_seconds": 0},
                )
                assert result["meta"] == {"status": "SUCCESS", "wait_reason": reason}, (
                    states,
                    branch,
                    settle,
                    result,
                )
                actual = records(result)
                if branch == "works":
                    assert actual == [
                        {
                            "agent": agent_target(a),
                            "work": work_target(w),
                            "work_status": state,
                        }
                        for (a, w), state in zip(pairs, states, strict=True)
                    ]
                else:
                    assert actual == [
                        {
                            "agent": agent_target(a),
                            "is_idle": state in {"completed", "stopped"},
                        }
                        for (a, _w), state in zip(pairs, states, strict=True)
                    ]
                assert h.runtime.suspensions == 0

    asyncio.run(scenario())


async def entered_wait(h, count=1):
    async with asyncio.timeout(1):
        while h.runtime.waiters < count:
            await asyncio.sleep(0)


@pytest.mark.parametrize("branch", ["works", "agents"])
def test_exact_completed_work_and_later_agent_work_have_distinct_waits(branch):
    async def scenario():
        h = harness()
        a, w0 = h.add_work(status="completed")
        b, w1 = h.add_work()
        targets = (
            [work_target(w0), work_target(w1)]
            if branch == "works"
            else [agent_target(a), agent_target(b)]
        )
        waiter = asyncio.create_task(h.call("wait_agent", {branch: targets}))
        await entered_wait(h)
        _, w2 = h.add_work(agent=a)
        h.repo.rows[w1]["status"] = "completed"
        await h.runtime.changed()
        if branch == "agents":
            await entered_wait(h, 2)
            assert not waiter.done()
            h.repo.rows[w2]["status"] = "completed"
            await h.runtime.changed()
        result = await asyncio.wait_for(waiter, 1)
        assert result["meta"]["wait_reason"] == "condition_met"
        if branch == "works":
            assert [row["work"] for row in records(result)] == targets
            assert all(row["work_status"] == "completed" for row in records(result))
            assert h.repo.rows[w2]["status"] == "running"
        else:
            assert records(result) == [
                {"agent": agent_target(a), "is_idle": True},
                {"agent": agent_target(b), "is_idle": True},
            ]

    asyncio.run(scenario())


def test_first_returns_every_exact_work_when_a_queued_peer_starts_later():
    async def scenario():
        h = harness()
        a, w1 = h.add_work()
        b, w2 = h.add_work(status="queued")
        waiter = asyncio.create_task(
            h.call(
                "wait_agent",
                {"works": [work_target(w1), work_target(w2)], "settle": "first"},
            )
        )
        await entered_wait(h)
        h.repo.rows[w2]["status"] = "running"
        h.repo.rows[w1]["status"] = "stopped"
        await h.runtime.changed()
        result = await asyncio.wait_for(waiter, 1)
        assert result["meta"]["wait_reason"] == "condition_met"
        assert records(result) == [
            {
                "agent": agent_target(a),
                "work": work_target(w1),
                "work_status": "stopped",
            },
            {
                "agent": agent_target(b),
                "work": work_target(w2),
                "work_status": "running",
            },
        ]

    asyncio.run(scenario())


def test_idle_all_does_not_accumulate_idle_or_ignore_new_queue_items():
    async def scenario():
        h = harness()
        a, w1 = h.add_work()
        b, w2 = h.add_work()
        waiter = asyncio.create_task(
            h.call("wait_agent", {"agents": [agent_target(a), agent_target(b)]})
        )
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
        assert result["meta"]["wait_reason"] == "condition_met"
        assert records(result) == [
            {"agent": agent_target(a), "is_idle": True},
            {"agent": agent_target(b), "is_idle": True},
        ]

    asyncio.run(scenario())


def test_idle_sender_message_wakes_all_without_finishing_the_other_agent():
    async def scenario():
        h = harness()
        a, _ = h.add_work(status="completed")
        b, _ = h.add_work()
        waiter = asyncio.create_task(
            h.call("wait_agent", {"agents": [agent_target(a), agent_target(b)]})
        )
        await entered_wait(h)
        h.runtime.messages[a] = 1
        await h.runtime.changed()
        result = await asyncio.wait_for(waiter, 1)
        assert result["meta"] == {
            "status": "SUCCESS",
            "wait_reason": "message_available",
            "message_sources": [agent_target(a)],
        }
        assert records(result) == [
            {"agent": agent_target(a), "is_idle": True},
            {"agent": agent_target(b), "is_idle": False},
        ]

    asyncio.run(scenario())


def test_control_precedes_message_and_message_precedes_condition_with_delivery_only_cursor():
    async def scenario():
        h = harness()
        a, _ = h.add_work(status="completed")
        h.runtime.messages[a] = 1
        args = {"agents": [agent_target(a)], "timeout_seconds": 0}
        h.context.cell_cancel_event.set()
        assert (await h.call("wait_agent", args))["meta"]["status"] == "CANCELLED"
        assert h.context.agent_message_cursors == {}
        h.context.cell_cancel_event.clear()
        result = await h.api.invoke(
            tool_name="wait_agent", arguments=args, invocation_context=h.context
        )
        assert result.public_value()["meta"]["wait_reason"] == "message_available"
        assert (await h.call("wait_agent", args))["meta"][
            "wait_reason"
        ] == "message_available"
        assert h.context.agent_message_cursors == {}
        await result.delivery_succeeded()
        assert (await h.call("wait_agent", args))["meta"][
            "wait_reason"
        ] == "condition_met"

    asyncio.run(scenario())


def test_undelivered_message_notice_leaves_actual_cursor_and_wait_snapshot_unchanged():
    async def scenario():
        h = harness()
        a, _ = h.add_work()
        h.runtime.messages[a] = 1
        args = {"agents": [agent_target(a)], "timeout_seconds": 0}
        first = await h.api.invoke(
            tool_name="wait_agent", arguments=args, invocation_context=h.context
        )
        second = await h.api.invoke(
            tool_name="wait_agent", arguments=args, invocation_context=h.context
        )
        assert first.public_value() == second.public_value()
        assert h.context.agent_message_cursors == {}
        assert h.runtime.suspensions == 0
        await second.delivery_succeeded()
        assert h.context.agent_message_cursors == {a: 1}
        assert (await h.call("wait_agent", args))["meta"]["wait_reason"] == "timeout"

    asyncio.run(scenario())


def test_change_between_initial_read_and_suspend_is_not_lost():
    async def scenario():
        h = harness()
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
        result = await asyncio.wait_for(
            h.call("wait_agent", {"agents": [agent_target(a)]}), 1
        )
        assert result["meta"]["wait_reason"] == "condition_met" and reads == 2

    asyncio.run(scenario())


def test_disappearing_target_returns_error_with_known_snapshot_instead_of_idle():
    async def scenario():
        h = harness()
        a, _ = h.add_work()
        waiter = asyncio.create_task(
            h.call("wait_agent", {"agents": [agent_target(a)]})
        )
        await entered_wait(h)
        h.repo.agents.remove(a)
        await h.runtime.changed()
        result = await asyncio.wait_for(waiter, 1)
        assert result["meta"]["status"] == "ERROR"
        assert records(result) == [{"agent": agent_target(a), "is_idle": False}]
        assert "does not exist" in result["content"][-1]["value"]

    asyncio.run(scenario())
