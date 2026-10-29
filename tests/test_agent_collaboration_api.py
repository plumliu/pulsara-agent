"""Observable collaboration contracts, without a second execution engine."""

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
import asyncio
import unittest

from pulsara_agent.identity import random_id
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import (
    agent_target,
    work_target,
    prepared_request,
    prepare_batch_refs,
    validate_batch_identities,
    validate_agent_request_graph,
    validate_collaboration_parameters,
)
from pulsara_agent.conversation_kernel.subagents.collaboration_api import (
    CollaborationToolPort,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.repository import ConversationKernelConflict


class ContractTests(unittest.TestCase):
    def test_removed_parameters_and_shapes_are_rejected(self):
        for name, arguments in [
            ("run_agent", {"instruction": "x", "model": {}}),
            (
                "run_agent",
                {
                    "instruction": "x",
                    "context": {"mode": "worker_history", "task_id": random_id("task")},
                },
            ),
            ("run_agent", {"instruction": "x", "target": agent_target("ROOT")}),
            (
                "run_agent",
                {
                    "instruction": "x",
                    "target": agent_target(random_id("session")),
                    "name": "rename",
                },
            ),
            ("wait_agent", {"targets": [work_target(random_id("queue"))]}),
            ("wait_agent", {"targets": []}),
            (
                "wait_agent.works",
                {"targets": [work_target(random_id("queue"))], "timeout_seconds": 1},
            ),
            (
                "stop_agent",
                {"target": agent_target(random_id("session")), "reason": "x"},
            ),
        ]:
            with (
                self.subTest(name=name, arguments=arguments),
                self.assertRaises(ValueError),
            ):
                validate_collaboration_parameters(name, arguments)

    def test_existing_per_call_utf8_boundaries_remain_strict(self):
        for name, arguments in [
            ("run_agent", {"instruction": "x" * 65_537}),
            ("run_agent", {"instruction": "valid", "name": "中" * 86}),
            (
                "send_agent_message",
                {"target": agent_target("ROOT"), "message": "x" * 16_385},
            ),
        ]:
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(ValueError, "UTF-8 byte boundary"),
            ):
                validate_collaboration_parameters(name, arguments)

    def test_prepared_ids_are_exact_and_hook_target_change_rejects_batch(self):
        peers = prepare_batch_refs({"research": {}, "review": {}})
        args = {
            "members": [
                dict(
                    key=key,
                    agent_id=ref["agent"]["agent_id"],
                    work_id=ref["work"]["work_id"],
                    parameters={
                        "instruction": f"Coordinate with {peers['research']['agent']}"
                    },
                )
                for key, ref in peers.items()
            ]
        }
        validate_batch_identities(args, peers)
        args["members"][1]["parameters"]["target"] = agent_target(random_id("session"))
        with self.assertRaisesRegex(ValueError, "target changed"):
            validate_batch_identities(args, peers)

    def test_fifo_and_success_dependencies_are_checked_together(self):
        agent = random_id("session")
        first = prepared_request(
            {"target": agent_target(agent), "instruction": "first"}
        )
        second = prepared_request(
            {"target": agent_target(agent), "instruction": "second"}
        )
        validate_agent_request_graph(
            (first, replace(second, depends_on=(first.work_id,)))
        )
        with self.assertRaisesRegex(ValueError, "waiting cycle"):
            validate_agent_request_graph(
                (replace(first, depends_on=(second.work_id,)), second)
            )
        old = (random_id("queue"), agent, (second.work_id,))
        with self.assertRaisesRegex(ValueError, "waiting cycle"):
            validate_agent_request_graph((second,), existing=(old,))

    def test_long_fifo_has_no_history_or_recursion_cap(self):
        agent = random_id("session")
        requests = tuple(
            prepared_request({"target": agent_target(agent), "instruction": "continue"})
            for _ in range(1500)
        )
        validate_agent_request_graph(requests)


class RepositoryOwner:
    def __init__(self, root):
        self.root = root
        self.rows = {}
        self.agents = set()
        self.admissions = []
        self.reject = False

    def query_agent_work(self, *, root_session_id, work_id, deadline_monotonic):
        return self.rows.get(work_id)

    def list_agent_works(
        self, *, root_session_id, agent_id, cursor=None, limit=20, deadline_monotonic
    ):
        if agent_id not in self.agents:
            raise ValueError("Agent does not belong to ROOT")
        rows = [r for r in self.rows.values() if r["agent_id"] == agent_id]
        if cursor:
            rows = rows[
                next(i for i, r in enumerate(rows) if r["work_id"] == cursor) + 1 :
            ]
        return tuple(rows[:limit])

    def read_agent_runtime_state(self, *, session_id, deadline_monotonic):
        if session_id not in self.agents:
            return None
        rows = [row for row in self.rows.values() if row["agent_id"] == session_id]
        current = next(
            (row["work_id"] for row in rows if row["status"] == "running"), None
        )
        return dict(
            agent_id=session_id,
            root_session_id=self.root,
            current_work_id=current,
            has_pending_work=any(row["status"] == "queued" for row in rows),
            latest_work_id=rows[-1]["work_id"] if rows else None,
            queued_count=sum(row["status"] == "queued" for row in rows),
            title="Agent",
        )

    def list_agent_sessions(
        self, *, root_session_id, cursor=None, limit=20, deadline_monotonic
    ):
        ids = sorted(self.agents)
        if cursor is not None:
            ids = [identity for identity in ids if identity > cursor]
        return tuple(
            dict(
                self.read_agent_runtime_state(
                    session_id=identity, deadline_monotonic=deadline_monotonic
                ),
                cursor=identity,
            )
            for identity in ids[:limit]
        )

    def list_active_agent_sessions(
        self, *, root_session_id, cursor=None, limit=50, deadline_monotonic
    ):
        rows = self.list_agent_sessions(
            root_session_id=root_session_id, cursor=cursor,
            limit=len(self.agents) or 1, deadline_monotonic=deadline_monotonic,
        )
        return tuple(
            row for row in rows
            if row["current_work_id"] is not None or row["queued_count"] > 0
        )[:limit]

    def admit_agent_requests(self, guard, *, requests, **kwargs):
        self.admissions.append(requests)
        if self.reject:
            raise ValueError("atomic owner rejected the batch")
        for request in requests:
            self.agents.add(request.agent_id)
            self.rows[request.work_id] = dict(
                agent_id=request.agent_id,
                work_id=request.work_id,
                instruction=request.instruction,
                status="queued",
                terminal_reason=None,
            )
        return tuple(self.rows[r.work_id] for r in requests)

    def confirm_agent_requests(self, guard, *, requests, **kwargs):
        return (
            tuple(self.rows[r.work_id] for r in requests)
            if all(r.work_id in self.rows for r in requests)
            else None
        )

    def cancel_agent_requests(self, guard, *, agent_id, work_id, **kwargs):
        rows = tuple(
            r
            for r in self.rows.values()
            if r["agent_id"] == agent_id
            and (work_id is None or r["work_id"] == work_id)
            and r["status"] not in {"completed", "stopped"}
        )
        for row in rows:
            if row["status"] == "queued":
                row.update(status="stopped", terminal_reason="USER_CANCELLED")
        return rows


class RuntimeOwner:
    def __init__(self, repo):
        self.repo = repo
        self.revision = 0
        self.condition = asyncio.Condition()
        self.messages = {}
        self.wakes = 0
        self.waiters = 0
        self.suspended = 0

    @asynccontextmanager
    async def request_admission(self, root_session_id, requests):
        yield

    def read_deadline(self):
        return 1e20

    def agent_change_revision(self):
        return self.revision

    async def wait_for_agent_change(self, revision):
        self.waiters += 1
        async with self.condition:
            await self.condition.wait_for(lambda: self.revision != revision)

    async def changed(self):
        async with self.condition:
            self.revision += 1
            self.condition.notify_all()

    async def wake_agent_requests(self, root):
        self.wakes += 1
        await self.changed()

    @asynccontextmanager
    async def suspend_execution(self, context):
        self.suspended += 1
        try:
            yield
        finally:
            self.suspended -= 1

    async def pending_message_sources(
        self, *, source_agent_ids, after_by_sender, **kwargs
    ):
        return {
            key: seq
            for key, seq in self.messages.items()
            if key in source_agent_ids and seq > after_by_sender.get(key, -1)
        }

    async def send_agent_steer(self, *, agent_id, message, source_context):
        rows = [row for row in self.repo.rows.values() if row["agent_id"] == agent_id]
        current = next((row for row in rows if row["status"] == "running"), None)
        return dict(
            recipient=agent_target(agent_id),
            work=work_target(current["work_id"]) if current else None,
            delivery="queued"
            if current
            else "not_started"
            if any(r["status"] == "queued" for r in rows)
            else "inactive",
            message="accepted" if current else "not accepted",
        )

    async def stop_active_work(self, *, agent_id, work_id):
        self.repo.rows[work_id].update(
            status="stopped", terminal_reason="USER_CANCELLED"
        )
        await self.changed()


class FacadeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = random_id("session")
        self.repo = RepositoryOwner(self.root)
        self.runtime = RuntimeOwner(self.repo)
        self.context = SimpleNamespace(
            session_id=self.root,
            turn_id=random_id("turn"),
            attempt_id=random_id("attempt"),
            effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            invocation_source=SimpleNamespace(kind="CODE_MODE_CALL"),
            agent_invocation_snapshot=SimpleNamespace(
                model_call_binding=object(),
                permission_snapshot=object(),
                initial_context_units=(),
            ),
            agent_message_cursors={},
            steer_event=asyncio.Event(),
        )
        self.api = CollaborationToolPort(
            repository=self.repo,
            root_session_id=self.root,
            session_id=self.root,
            runtime=self.runtime,
            io=KernelSessionIO(),
            admission_facts=lambda context, requests: {
                "root_guard": object(),
                "model_call_binding": object(),
                "permission_snapshot": object(),
                "actor_id": "test",
                "deadline_monotonic": 1e20,
            },
        )

    def add_work(self, *, agent=None, status="running", final=None):
        agent = agent or random_id("session")
        self.repo.agents.add(agent)
        work = random_id("queue")
        self.repo.rows[work] = dict(
            work_id=work,
            agent_id=agent,
            instruction="task",
            status=status,
            terminal_reason="TEST_STOP" if status == "stopped" else None,
            **({"final": final} if final is not None else {}),
        )
        return agent, work

    async def call(self, name, arguments):
        result = await self.api.invoke(
            tool_name=name, arguments=arguments, invocation_context=self.context
        )
        return result.public_value()

    async def test_batch_uses_one_admission_and_rejection_never_wakes_scheduler(self):
        peers = prepare_batch_refs({"a": {}, "b": {}})
        args = {
            "members": [
                dict(
                    key=key,
                    agent_id=ref["agent"]["agent_id"],
                    work_id=ref["work"]["work_id"],
                    parameters={"instruction": key},
                )
                for key, ref in peers.items()
            ]
        }
        self.repo.reject = True
        with self.assertRaises(ValueError):
            await self.call("run_agent.batch", args)
        self.assertEqual(self.runtime.wakes, 0)
        self.assertEqual(self.repo.rows, {})
        self.repo.reject = False
        result = await self.call("run_agent.batch", args)
        self.assertEqual(len(self.repo.admissions[-1]), 2)
        self.assertEqual(result["b"]["work"], peers["b"]["work"])
        self.assertEqual(self.runtime.wakes, 1)

    async def test_unknown_admission_commit_is_confirmed_without_repeating_submission(
        self,
    ):
        original = self.repo.admit_agent_requests
        calls = []

        def committed_then_lost(*args, **kwargs):
            calls.append(None)
            original(*args, **kwargs)
            raise ConnectionError("commit acknowledgement lost")

        self.repo.admit_agent_requests = committed_then_lost
        result = await self.call("run_agent", {"instruction": "submit exactly once"})
        self.assertEqual(result["status"], "queued")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.repo.rows), 1)
        self.assertEqual(self.runtime.wakes, 1)

    async def test_handoff_pages_only_active_agents_without_pseudo_agent_rows(self):
        for _ in range(51):
            self.add_work(status="queued")
        index = await self.api.freeze_compaction_handoff()
        self.assertEqual(len(index), 50)
        remaining = await self.api.freeze_compaction_handoff(
            cursor=index[-1]["agent"]["agent_id"]
        )
        self.assertEqual(len(remaining), 1)
        self.assertTrue(all(item["queued_count"] == 1 for item in (*index, *remaining)))
        self.assertTrue(all(
            set(item) == {"agent", "current_work", "queued_count"}
            for item in (*index, *remaining)
        ))
        self.assertEqual(
            [item["agent"]["agent_id"] for item in (*index, *remaining)],
            sorted(self.repo.agents),
        )

    async def test_handoff_read_failure_propagates_and_subagent_has_no_inventory(self):
        failure = ConversationKernelConflict("active Agent read failed")

        def fail(**kwargs):
            raise failure

        self.repo.list_active_agent_sessions = fail
        with self.assertRaises(ConversationKernelConflict) as caught:
            await self.api.freeze_compaction_handoff()
        self.assertIs(caught.exception, failure)
        self.api.session_id = random_id("session")
        self.assertEqual(await self.api.freeze_compaction_handoff(), ())

    async def test_cancelled_physical_admission_preserves_the_accepted_result(self):
        import threading

        entered, release = threading.Event(), threading.Event()
        original = self.repo.admit_agent_requests

        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test failed to release physical operation")
            return original(*args, **kwargs)

        self.repo.admit_agent_requests = delayed
        task = asyncio.create_task(
            self.api.invoke(
                tool_name="run_agent",
                arguments={"instruction": "preserve accepted work"},
                invocation_context=self.context,
            )
        )
        while not entered.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        result = await asyncio.wait_for(task, 1)
        self.assertTrue(result.caller_cancelled_while_running)
        self.assertEqual(result.public_value()["status"], "queued")
        self.assertEqual(len(self.repo.rows), 1)

    async def test_queued_agent_message_and_native_wait_do_not_start_work(self):
        agent, work = self.add_work(status="queued")
        delivery = await self.call(
            "send_agent_message", {"target": agent_target(agent), "message": "question"}
        )
        self.assertEqual(delivery["delivery"], "not_started")
        result = await self.call(
            "wait_agent", {"targets": [agent_target(agent)], "timeout_seconds": 0}
        )
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["unavailable_targets"][0]["reason"], "not_started")
        self.assertEqual(self.repo.rows[work]["status"], "queued")
        self.assertEqual(self.runtime.wakes, 0)

    async def test_wait_binds_original_work_and_never_follows_next_request(self):
        agent, original = self.add_work()
        waiter = asyncio.create_task(
            self.call("wait_agent", {"targets": [agent_target(agent)]})
        )
        while not self.runtime.waiters:
            await asyncio.sleep(0)
        self.repo.rows[original].update(status="completed", final="original final")
        _, next_work = self.add_work(agent=agent)
        await self.runtime.changed()
        result = await asyncio.wait_for(waiter, 1)
        self.assertEqual(result["state"], "settled")
        self.assertEqual(result["bindings"][0]["work"], work_target(original))
        self.assertEqual(result["works"][0]["final"], "original final")
        self.assertEqual(self.repo.rows[next_work]["status"], "running")

    async def test_message_wakes_agent_wait_without_copying_body_or_skipping_other_senders(
        self,
    ):
        a, _ = self.add_work()
        b, _ = self.add_work()
        self.runtime.messages[a] = 9
        first = await self.call(
            "wait_agent",
            {"targets": [agent_target(a), agent_target(b)], "timeout_seconds": 0},
        )
        self.assertEqual(first["state"], "message_available")
        self.assertEqual(first["message_sources"], [agent_target(a)])
        self.runtime.messages[b] = 2
        second = await self.call(
            "wait_agent",
            {"targets": [agent_target(a), agent_target(b)], "timeout_seconds": 0},
        )
        self.assertEqual(second["message_sources"], [agent_target(b)])
        third = await self.call(
            "wait_agent",
            {"targets": [agent_target(a), agent_target(b)], "timeout_seconds": 0},
        )
        self.assertEqual(third["state"], "timeout")

    async def test_one_message_wakes_concurrent_observers_without_consumption(self):
        agent, _ = self.add_work()
        arguments = {"targets": [agent_target(agent)], "timeout_seconds": 30}
        first = asyncio.create_task(self.call("wait_agent", arguments))
        second = asyncio.create_task(self.call("wait_agent", arguments))
        while self.runtime.waiters < 2:
            await asyncio.sleep(0)
        self.runtime.messages[agent] = 1
        await self.runtime.changed()
        results = await asyncio.wait_for(asyncio.gather(first, second), 1)
        self.assertEqual(
            [r["state"] for r in results], ["message_available", "message_available"]
        )
        later = await self.call(
            "wait_agent", {"targets": [agent_target(agent)], "timeout_seconds": 0}
        )
        self.assertEqual(later["state"], "timeout")

    async def test_work_wait_ignores_messages_and_returns_real_stopped_state(self):
        agent, work = self.add_work(status="queued")
        waiter = asyncio.create_task(
            self.call("wait_agent.works", {"targets": [work_target(work)]})
        )
        while not self.runtime.waiters:
            await asyncio.sleep(0)
        self.runtime.messages[agent] = 1
        await self.runtime.changed()
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        self.repo.rows[work].update(
            status="stopped", terminal_reason="DEPENDENCY_NOT_SUCCESSFUL"
        )
        await self.runtime.changed()
        result = await asyncio.wait_for(waiter, 1)
        self.assertEqual(result["state"], "settled")
        self.assertEqual(result["works"][0]["status"], "stopped")
        self.assertNotIn("final", result["works"][0])
        self.assertEqual(self.runtime.suspended, 0)

    async def test_cell_control_interrupts_work_observation_without_stopping_work(self):
        _, work = self.add_work(status="queued")
        waiter = asyncio.create_task(
            self.call("wait_agent.works", {"targets": [work_target(work)]})
        )
        while not self.runtime.waiters:
            await asyncio.sleep(0)
        self.context.steer_event.set()
        result = await asyncio.wait_for(waiter, 1)
        self.assertEqual(result["state"], "interrupted")
        self.assertEqual(result["pending_targets"], [work_target(work)])
        self.assertEqual(self.repo.rows[work]["status"], "queued")
        self.assertEqual(self.runtime.suspended, 0)

    async def test_unselected_agent_message_does_not_interrupt_but_human_control_does(
        self,
    ):
        selected, work = self.add_work()
        other, _ = self.add_work()
        waiter = asyncio.create_task(
            self.call(
                "wait_agent",
                {"targets": [agent_target(selected)], "timeout_seconds": 300},
            )
        )
        while not self.runtime.waiters:
            await asyncio.sleep(0)
        self.runtime.messages[other] = 1
        await self.runtime.changed()
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        self.context.steer_event.set()
        result = await asyncio.wait_for(waiter, 1)
        self.assertEqual(result["state"], "interrupted")
        self.assertEqual(self.repo.rows[work]["status"], "running")
        self.assertNotIn(other, self.context.agent_message_cursors)

    async def test_work_stop_does_not_cancel_unrelated_queued_work(self):
        agent, running = self.add_work()
        _, queued = self.add_work(agent=agent, status="queued")
        _, completed = self.add_work(agent=agent, status="completed", final="kept")
        result = await self.call("stop_agent", {"target": work_target(running)})
        self.assertEqual(result["works"][0]["status"], "stopped")
        self.assertEqual(self.repo.rows[queued]["status"], "queued")
        ended = await self.call("stop_agent", {"target": work_target(completed)})
        self.assertFalse(ended["stopped"])
        self.assertEqual(ended["works"][0]["final"], "kept")
