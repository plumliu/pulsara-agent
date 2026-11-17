"""Observable collaboration contracts, without a second execution engine."""

from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from types import SimpleNamespace
import asyncio
import threading
import unittest
from unittest.mock import patch

from pulsara_agent.identity import random_id
from tests.test_agent_collaboration_array_contracts import envelope
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import (
    agent_target,
    work_target,
    prepared_request,
    collaboration_output_schema,
    validate_batch_identities,
    validate_agent_request_graph,
    validate_collaboration_parameters,
)
from pulsara_agent.conversation_kernel.subagents.collaboration_api import (
    CollaborationToolPort,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO


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
            ("wait_agent", {"agents": []}),
            (
                "wait_work",
                {"works": [work_target(random_id("queue"))], "timeout_seconds": 1},
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
            ("run_agent", envelope([{"instruction": "x" * 65_537}])[1]),
            (
                "run_agent",
                envelope([{"instruction": "valid", "work_title": "中" * 86}])[1],
            ),
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
        peers, args = envelope([{"instruction": "research"}, {"instruction": "review"}])
        args["members"][0]["parameters"]["instruction"] = (
            f"Coordinate with {peers['agents'][0]}"
        )
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


class SummaryRepositoryTests(unittest.TestCase):
    def test_work_directory_never_hydrates_canonical_prompt_or_final(self):
        from pulsara_agent.conversation_kernel._repository.subagents import (
            _SubagentOperations,
        )

        agent, work, root = (
            random_id("session"),
            random_id("queue"),
            random_id("session"),
        )

        class Connection:
            def execute(self, sql, parameters):
                return SimpleNamespace(
                    fetchone=lambda: {"exists": True},
                    fetchall=lambda: [
                        {"id": work, "session_id": agent, "work_title": None}
                    ],
                )

        @contextmanager
        def connection(**kwargs):
            yield Connection()

        repository = _SubagentOperations()
        repository._provider = SimpleNamespace(connection=connection)
        repository._agent_work_value = lambda *_: self.fail(
            "inventory must not hydrate a Work body"
        )
        with patch(
            "pulsara_agent.conversation_kernel._repository.subagents.request_execution_from_row",
            return_value=SimpleNamespace(
                status=SimpleNamespace(value="completed"), terminal_reason=None
            ),
        ):
            rows = repository.list_query_agent_works(
                root_session_id=root,
                observer_session_id=root,
                agent_id=agent,
                deadline_monotonic=1e20,
            )
        self.assertEqual(
            rows,
            (
                {
                    "work_id": work,
                    "agent_id": agent,
                    "status": "completed",
                    "terminal_reason": None,
                    "work_title": None,
                    "cursor": work,
                },
            ),
        )


class RepositoryOwner:
    def __init__(self, root):
        self.root = root
        self.rows = {}
        self.agents = set()
        self.known = {}
        self.admissions = []
        self.reject = False

    def query_agent_work(self, *, root_session_id, work_id, deadline_monotonic):
        return self.rows.get(work_id)

    def read_agent_work_graph_facts(
        self, *, root_session_id, work_id, deadline_monotonic
    ):
        row = self.rows.get(work_id)
        return (
            None
            if row is None
            else {
                key: row[key]
                for key in (
                    "work_id",
                    "agent_id",
                    "status",
                    "terminal_reason",
                    "work_title",
                )
                if key in row
            }
        )

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
            ui_current_work_id=current,
            ui_current_work_title=None
            if current is None
            else self.rows[current]["work_title"],
            ui_current_work_status=None
            if current is None
            else self.rows[current]["status"],
            ui_queued_work_count=sum(row["status"] == "queued" for row in rows),
        )

    def read_work_wait_states(self, *, root_session_id, work_ids, deadline_monotonic):
        return tuple(
            {key: self.rows[work][key] for key in ("agent_id", "work_id", "status")}
            for work in work_ids
            if work in self.rows
        )

    def read_agent_wait_states(
        self, *, root_session_id, agent_ids, bound_work_ids, deadline_monotonic
    ):
        rows = []
        for identity, work_id in zip(agent_ids, bound_work_ids, strict=True):
            state = self.read_agent_runtime_state(
                session_id=identity, deadline_monotonic=deadline_monotonic
            )
            if state is None or state["root_session_id"] != root_session_id:
                continue
            work = self.rows.get(work_id)
            rows.append(
                dict(
                    agent_id=identity,
                    current_work_id=state["current_work_id"],
                    has_running_turn=state["current_work_id"] is not None,
                    has_pending_work=state["has_pending_work"],
                    bound_work_id=None if work is None else work["work_id"],
                    bound_work_status=None if work is None else work["status"],
                )
            )
        return tuple(rows)

    def _session_rows(
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

    def _query_visible(self, observer_session_id, agent_id):
        return agent_id in self.agents and (
            observer_session_id == self.root
            or agent_id == observer_session_id
            or agent_id in self.known.get(observer_session_id, set())
        )

    def list_query_agent_sessions(
        self,
        *,
        root_session_id,
        observer_session_id,
        cursor=None,
        limit=50,
        active_only=False,
        deadline_monotonic,
    ):
        rows = self._session_rows(
            root_session_id=root_session_id,
            cursor=cursor,
            limit=len(self.agents) or 1,
            deadline_monotonic=deadline_monotonic,
        )
        return tuple(
            row
            for row in rows
            if self._query_visible(observer_session_id, row["agent_id"])
            and (
                not active_only
                or row["current_work_id"] is not None
                or row["queued_count"] > 0
            )
        )[:limit]

    def list_query_agent_works(self, *, observer_session_id, agent_id, **kwargs):
        if not self._query_visible(observer_session_id, agent_id):
            raise ValueError("AGENT does not exist in this collaboration")
        return self.list_agent_works(agent_id=agent_id, **kwargs)

    def query_visible_agent_work(self, *, observer_session_id, **kwargs):
        row = self.query_agent_work(**kwargs)
        return (
            row
            if row and self._query_visible(observer_session_id, row["agent_id"])
            else None
        )

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
                work_title=request.work_title,
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
        self.suspensions = 0

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
        self.suspensions += 1
        try:
            yield
        finally:
            self.suspended -= 1

    async def has_pending_agent_message(self, *, invocation_context):
        return bool(self.messages)

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


class InboxWaitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FacadeTests.setUp(self)
        self.api.session_id = self.context.session_id = random_id("session")
        self.context.invocation_source = SimpleNamespace(kind="MODEL_TOOL_CALL")
        self.context.cell_cancel_event = None
        self.context.agent_message_cursors = None

    async def call(self, name, arguments):
        return await self.api._wait_message(arguments, self.context)

    async def test_poll_and_existing_messages_do_not_yield_a_slot(self):
        self.assertEqual(
            await self.call("wait_message", {"timeout_seconds": 0}),
            {"state": "timeout"},
        )
        self.runtime.messages[self.root] = 1
        self.assertEqual(
            await self.call("wait_message", {}), {"state": "message_available"}
        )
        self.assertEqual(self.runtime.suspensions, 0)
        self.assertEqual(self.runtime.messages, {self.root: 1})

    async def test_fast_checks_keep_a_saturated_real_runtime_slot(self):
        from pulsara_agent.conversation_kernel.subagent import AgentSessionRuntime

        slots = AgentSessionRuntime(None)
        await slots.set_capacity_target(1)
        self.assertTrue(await slots.try_acquire(self.context.session_id))
        competitor = random_id("session")
        original_release = slots.release
        released = []

        async def competing_release(session_id):
            released.append(session_id)
            await original_release(session_id)
            self.assertTrue(await slots.try_acquire(competitor))

        slots.release = competing_release
        self.runtime.suspend_execution = slots.suspend_execution
        self.assertEqual(
            await asyncio.wait_for(
                self.call("wait_message", {"timeout_seconds": 0}), 1
            ),
            {"state": "timeout"},
        )
        self.runtime.messages[self.root] = 1
        self.assertEqual(
            await asyncio.wait_for(self.call("wait_message", {}), 1),
            {"state": "message_available"},
        )
        self.assertEqual(released, [])
        self.assertFalse(await slots.try_acquire(competitor))
        self.assertEqual(await slots.capacity_state(), (1, 1))

    async def test_any_sender_wakes_and_unrelated_changes_do_not(self):
        waiter = asyncio.create_task(self.call("wait_message", {}))
        while not self.runtime.waiters:
            await asyncio.sleep(0)
        await self.runtime.changed()
        while self.runtime.waiters < 2:
            await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        self.runtime.messages[random_id("session")] = 1
        self.runtime.messages[self.root] = 2
        await self.runtime.changed()
        self.assertEqual(
            await asyncio.wait_for(waiter, 1), {"state": "message_available"}
        )
        self.assertEqual(len(self.runtime.messages), 2)
        self.assertEqual(self.runtime.suspended, 0)

    async def test_arrival_between_first_check_and_suspend_is_not_missed(self):
        async def query(*, invocation_context):
            if not self.runtime.messages:
                self.runtime.messages[self.root] = 1
                await self.runtime.changed()
                return False
            return True

        self.runtime.has_pending_agent_message = query
        self.assertEqual(
            await asyncio.wait_for(self.call("wait_message", {}), 1),
            {"state": "message_available"},
        )
        self.assertEqual(self.runtime.suspensions, 1)

    async def test_cancel_releases_wait_and_does_not_consume_mail(self):
        waiter = asyncio.create_task(self.call("wait_message", {}))
        while not self.runtime.waiters:
            await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(self.runtime.suspended, 0)
        self.runtime.messages[self.root] = 1
        self.assertEqual(
            await self.call("wait_message", {}), {"state": "message_available"}
        )

    async def test_unrelated_changes_do_not_extend_deadline(self):
        waiter = asyncio.create_task(
            self.call("wait_message", {"timeout_seconds": 0.03})
        )

        async def notify():
            while not waiter.done():
                await self.runtime.changed()
                await asyncio.sleep(0.001)

        notifier = asyncio.create_task(notify())
        try:
            self.assertEqual(await asyncio.wait_for(waiter, 1), {"state": "timeout"})
        finally:
            await notifier
        self.assertEqual(self.runtime.suspended, 0)


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
            cell_cancel_event=asyncio.Event(),
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
            work_title=None,
            status=status,
            terminal_reason="TEST_STOP" if status == "stopped" else None,
            **({"final": final} if final is not None else {}),
        )
        return agent, work

    async def call(self, name, arguments):
        result = await self.api.invoke(
            tool_name=name, arguments=arguments, invocation_context=self.context
        )
        value = result.public_value()
        from jsonschema import Draft202012Validator
        Draft202012Validator(collaboration_output_schema(name)).validate(value)
        return value

    @staticmethod
    def records(value):
        return [part["value"] for part in value["content"] if part["type"] == "object"]

    async def test_complete_queries_preserve_scope(self):
        agent = random_id("session")
        expected = [
            self.add_work(agent=agent, status="completed", final="x" * 100_000)[1]
            for _ in range(71)
        ]
        result = await self.call("query_agents", {"target": agent_target(agent)})
        self.assertEqual(
            [row["target"] for row in self.records(result)],
            [work_target(work) for work in expected],
        )
        detail = await self.call("query_agents", {"target": work_target(expected[0])})
        self.assertEqual(self.records(detail)[0]["final"], "x" * 100_000)
        self.context.session_id = random_id("session")
        self.assertEqual(self.records(await self.call("query_agents", {})), [])
        with self.assertRaises(ValueError):
            await self.call("query_agents", {"target": work_target(expected[0])})

    async def test_dispatch_preserves_positions_and_new_or_reused_history_selection(
        self,
    ):
        self.context.agent_invocation_snapshot.initial_context_units = (
            "old",
            "middle",
            "latest",
        )
        _, arguments = envelope(
            [
                {
                    "instruction": "first",
                    "materials": [{"kind": "CONTEXT_MATERIAL", "turns": 2}],
                }
            ]
        )
        first = await self.call("run_agent", arguments)
        record = self.records(first)[0]
        self.assertEqual(set(record), {"agent", "work", "work_status"})
        self.assertEqual(
            self.repo.admissions[-1][0].initial_context, ("middle", "latest")
        )
        _, arguments = envelope(
            [
                {
                    "target": record["agent"],
                    "instruction": "second",
                    "materials": [{"kind": "CONTEXT_MATERIAL", "turns": 1}],
                }
            ]
        )
        second = await self.call("run_agent", arguments)
        self.assertEqual(self.records(second)[0]["agent"], record["agent"])
        self.assertNotEqual(self.records(second)[0]["work"], record["work"])
        self.assertEqual(self.repo.admissions[-1][0].initial_context, ("latest",))

    async def test_atomic_rejection_does_not_wake_or_partially_admit(self):
        _, args = envelope([{"instruction": "one"}, {"instruction": "two"}])
        self.repo.reject = True
        with self.assertRaises(ValueError):
            await self.call("run_agent", args)
        self.assertEqual(self.repo.rows, {})
        self.assertEqual(self.runtime.wakes, 0)
        self.repo.reject = False
        accepted = await self.call("run_agent", args)
        self.assertEqual(len(self.records(accepted)), 2)
        self.assertEqual(len(self.repo.admissions[-1]), 2)

    async def test_ack_unknown_confirms_original_submission_without_resubmission(self):
        original = self.repo.admit_agent_requests
        calls = []

        def lost(*args, **kwargs):
            calls.append(None)
            original(*args, **kwargs)
            raise ConnectionError("acknowledgement lost")

        self.repo.admit_agent_requests = lost
        accepted = await self.call("run_agent", envelope([{"instruction": "one"}])[1])
        self.assertEqual(self.records(accepted)[0]["work_status"], "queued")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.repo.rows), 1)

    async def test_fast_completion_freezes_admission_record_and_detail_is_separate(
        self,
    ):
        original = self.repo.admit_agent_requests

        def finish(*args, **kwargs):
            rows = original(*args, **kwargs)
            for row in rows:
                row.update(status="completed", final="full report")
            return rows

        self.repo.admit_agent_requests = finish
        result = await self.call("run_agent", envelope([{"instruction": "quick"}])[1])
        record = self.records(result)[0]
        self.assertEqual(
            record,
            {
                "agent": agent_target(next(iter(self.repo.agents))),
                "work": work_target(next(iter(self.repo.rows))),
                "work_status": "completed",
            },
        )
        detail = await self.call("query_agents", {"target": record["work"]})
        self.assertEqual(self.records(detail)[0]["final"], "full report")

    async def test_message_to_queued_only_recipient_is_error_without_starting_work(
        self,
    ):
        agent, work = self.add_work(status="queued")
        result = await self.call(
            "send_agent_message", {"target": agent_target(agent), "message": "hello"}
        )
        self.assertEqual(result["meta"]["status"], "ERROR")
        self.assertEqual(self.repo.rows[work]["status"], "queued")
        self.repo.rows[work]["status"] = "running"
        result = await self.call(
            "send_agent_message",
            {"target": agent_target(agent), "message": "hello\nworld"},
        )
        self.assertEqual(
            result,
            {
                "meta": {"status": "SUCCESS"},
                "content": [
                    {"type": "text", "value": "消息已加入接收方当前轮次的输入队列。"}
                ],
            },
        )

    async def test_stop_lists_actual_selected_states_and_all_four_counts(self):
        agent, work = self.add_work(status="running")
        _, queued = self.add_work(agent=agent, status="queued")
        _, historical = self.add_work(agent=agent, status="completed", final="old")
        result = await self.call("stop_agent", {"target": agent_target(agent)})
        self.assertEqual(
            self.records(result),
            [
                {"target": work_target(work), "work_status": "stopped"},
                {"target": work_target(queued), "work_status": "stopped"},
            ],
        )
        self.assertEqual(
            result["meta"],
            {
                "status": "SUCCESS",
                "queued_work_count": 0,
                "running_work_count": 0,
                "completed_work_count": 0,
                "stopped_work_count": 2,
            },
        )
        terminal = await self.call("stop_agent", {"target": work_target(historical)})
        self.assertEqual(
            self.records(terminal),
            [{"target": work_target(historical), "work_status": "completed"}],
        )
        self.assertEqual(terminal["meta"]["completed_work_count"], 1)

    async def test_stop_failure_preserves_committed_queue_cancellation(self):
        agent, running = self.add_work(status="running")
        _, queued = self.add_work(agent=agent, status="queued")

        async def fail_stop(**kwargs):
            raise OSError("stop failed")

        self.runtime.stop_active_work = fail_stop
        result = await self.call("stop_agent", {"target": agent_target(agent)})
        self.assertEqual(result["meta"], {"status": "ERROR"})
        self.assertIn("stop failed", result["content"][0]["value"])
        self.assertEqual(self.records(result), [
            {"target": work_target(running), "work_status": "running"},
            {"target": work_target(queued), "work_status": "stopped"},
        ])
        self.assertEqual(self.repo.rows[queued]["status"], "stopped")

    async def test_stop_read_failure_keeps_new_and_transaction_observations(self):
        agent, running = self.add_work(status="running")
        _, queued = self.add_work(agent=agent, status="queued")
        original = self.api._work_status

        async def read_with_failure(work_id):
            if work_id == queued:
                raise OSError("read failed")
            return await original(work_id)

        self.api._work_status = read_with_failure
        result = await self.call("stop_agent", {"target": agent_target(agent)})
        self.assertEqual(result["meta"], {"status": "ERROR"})
        self.assertIn("read failed", result["content"][0]["value"])
        self.assertEqual(self.records(result), [
            {"target": work_target(running), "work_status": "stopped"},
            {"target": work_target(queued), "work_status": "stopped"},
        ])

    async def test_stop_cancel_after_commit_reports_known_effects(self):
        agent, running = self.add_work(status="running")
        _, queued = self.add_work(agent=agent, status="queued")
        entered = asyncio.Event()

        async def pending_stop(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.runtime.stop_active_work = pending_stop
        task = asyncio.create_task(self.api.invoke(
            tool_name="stop_agent",
            arguments={"target": agent_target(agent)},
            invocation_context=self.context,
        ))
        await entered.wait()
        task.cancel()
        outcome = await task
        self.assertTrue(outcome.caller_cancelled_while_running)
        self.assertEqual(outcome.physical_timing, "ON_TIME")
        self.assertIsNotNone(outcome.physical_observation)
        result = outcome.public_value()
        self.assertEqual(result["meta"], {"status": "CANCELLED"})
        self.assertEqual(self.records(result), [
            {"target": work_target(running), "work_status": "running"},
            {"target": work_target(queued), "work_status": "stopped"},
        ])
        self.assertEqual(self.repo.rows[queued]["status"], "stopped")

    async def test_stop_cancellation_during_transaction_drains_exact_result(self):
        agent, queued = self.add_work(status="queued")
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        cancel_requests = self.repo.cancel_agent_requests

        def delayed_cancel(*args, **kwargs):
            loop.call_soon_threadsafe(started.set)
            release.wait()
            return cancel_requests(*args, **kwargs)

        self.repo.cancel_agent_requests = delayed_cancel
        task = asyncio.create_task(self.api.invoke(
            tool_name="stop_agent",
            arguments={"target": agent_target(agent)},
            invocation_context=self.context,
        ))
        await started.wait()
        task.cancel()
        release.set()
        result = await task
        self.assertTrue(result.caller_cancelled_while_running)
        self.assertEqual(result.physical_timing, "LATE_AFTER_WATCHDOG")
        self.assertIsNotNone(result.physical_observation)
        self.assertEqual(result.public_value()["meta"]["status"], "SUCCESS")
        self.assertEqual(self.records(result.public_value()), [
            {"target": work_target(queued), "work_status": "stopped"},
        ])

    async def test_work_wait_uses_exact_snapshot_order_including_queued_and_historical(
        self,
    ):
        agent, first = self.add_work(status="completed")
        _, next_work = self.add_work(agent=agent, status="queued")
        result = await self.call(
            "wait_agent",
            {"works": [work_target(next_work), work_target(first)], "settle": "first"},
        )
        self.assertEqual(
            result["meta"], {"status": "SUCCESS", "wait_reason": "condition_met"}
        )
        self.assertEqual(
            [row["work"] for row in self.records(result)],
            [work_target(next_work), work_target(first)],
        )
        self.assertEqual(
            [row["work_status"] for row in self.records(result)],
            ["queued", "completed"],
        )
        self.runtime.messages[agent] = 5
        result = await self.call(
            "wait_agent", {"works": [work_target(next_work)], "timeout_seconds": 0}
        )
        self.assertEqual(result["meta"]["wait_reason"], "timeout")
        self.assertEqual(self.context.agent_message_cursors, {})

    async def test_exact_work_wait_never_follows_successor_or_hydrates_result(self):
        agent, work = self.add_work(status="running", final="secret")
        self.repo.query_agent_work = lambda **_: self.fail("wait hydrated a final")
        waiter = asyncio.create_task(
            self.call(
                "wait_agent", {"works": [work_target(work)], "timeout_seconds": 1}
            )
        )
        while not self.runtime.waiters:
            await asyncio.sleep(0)
        self.repo.rows[work]["status"] = "completed"
        self.add_work(agent=agent, status="running")
        await self.runtime.changed()
        result = await waiter
        self.assertEqual(result["meta"]["wait_reason"], "condition_met")
        self.assertEqual(
            self.records(result),
            [
                {
                    "agent": agent_target(agent),
                    "work": work_target(work),
                    "work_status": "completed",
                }
            ],
        )

    async def test_idle_is_one_current_snapshot_not_accumulated_observations(self):
        a, wa = self.add_work(status="completed")
        b, wb = self.add_work(status="running")
        waiter = asyncio.create_task(
            self.call(
                "wait_agent",
                {"agents": [agent_target(a), agent_target(b)], "timeout_seconds": 1},
            )
        )
        while not self.runtime.waiters:
            await asyncio.sleep(0)
        self.repo.rows[wa]["status"] = "running"
        self.repo.rows[wb]["status"] = "completed"
        await self.runtime.changed()
        while self.runtime.waiters < 2:
            await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        self.repo.rows[wa]["status"] = "completed"
        await self.runtime.changed()
        result = await waiter
        self.assertEqual(
            self.records(result),
            [
                {"agent": agent_target(a), "is_idle": True},
                {"agent": agent_target(b), "is_idle": True},
            ],
        )

    async def test_messages_follow_selected_input_order_and_advance_only_after_js_delivery(
        self,
    ):
        a, _ = self.add_work(status="completed")
        b, _ = self.add_work(status="running")
        self.runtime.messages = {b: 3, a: 2}
        args = {"agents": [agent_target(a), agent_target(b)], "timeout_seconds": 0}
        result = await self.api.invoke(
            tool_name="wait_agent", arguments=args, invocation_context=self.context
        )
        self.assertEqual(
            result.public_value()["meta"],
            {
                "status": "SUCCESS",
                "wait_reason": "message_available",
                "message_sources": [agent_target(a), agent_target(b)],
            },
        )
        self.assertEqual(self.context.agent_message_cursors, {})
        await result.delivery_succeeded()
        self.assertEqual(self.context.agent_message_cursors, {a: 2, b: 3})
        self.assertEqual(self.runtime.messages, {b: 3, a: 2})
        second = await self.call("wait_agent", args)
        self.assertEqual(second["meta"]["wait_reason"], "timeout")

    async def test_overlapping_waits_can_report_same_message_and_other_cells_are_independent(
        self,
    ):
        a, _ = self.add_work(status="running")
        args = {"agents": [agent_target(a)], "timeout_seconds": 0}
        self.runtime.messages[a] = 7
        results = await asyncio.gather(
            *[
                self.api.invoke(
                    tool_name="wait_agent",
                    arguments=args,
                    invocation_context=self.context,
                )
                for _ in range(2)
            ]
        )
        self.assertTrue(
            all(
                r.public_value()["meta"]["wait_reason"] == "message_available"
                for r in results
            )
        )
        for result in results:
            await result.delivery_succeeded()
        other = (
            replace(self.context)
            if False
            else SimpleNamespace(**{**vars(self.context), "agent_message_cursors": {}})
        )
        result = await self.api.invoke(
            tool_name="wait_agent", arguments=args, invocation_context=other
        )
        self.assertEqual(
            result.public_value()["meta"]["wait_reason"], "message_available"
        )

    async def test_cancel_takes_priority_and_does_not_read_again_or_stop_targets(self):
        a, work = self.add_work(status="running")
        self.runtime.messages[a] = 1
        self.context.cell_cancel_event.set()
        self.repo.read_agent_wait_states = lambda **_: self.fail(
            "cancelled wait re-read states"
        )
        result = await self.call(
            "wait_agent", {"agents": [agent_target(a)], "timeout_seconds": 0}
        )
        self.assertEqual(result["meta"]["status"], "CANCELLED")
        self.assertEqual(self.repo.rows[work]["status"], "running")
        self.assertEqual(self.context.agent_message_cursors, {})

    async def test_missing_target_is_error_and_stopped_work_is_normal_condition(self):
        result = await self.call(
            "wait_agent",
            {"works": [work_target(random_id("queue"))], "timeout_seconds": 0},
        )
        self.assertEqual(result["meta"]["status"], "ERROR")
        a, work = self.add_work(status="stopped")
        result = await self.call("wait_agent", {"works": [work_target(work)]})
        self.assertEqual(
            result["meta"], {"status": "SUCCESS", "wait_reason": "condition_met"}
        )
        self.assertEqual(self.records(result)[0]["work_status"], "stopped")

    async def test_wait_collection_has_no_dispatch_member_limit(self):
        works = [self.add_work(status="completed")[1] for _ in range(25)]
        result = await self.call(
            "wait_agent", {"works": [work_target(work) for work in works]}
        )
        self.assertEqual(len(self.records(result)), 25)

    async def test_root_native_and_subagent_js_wait_rejected_at_actual_owner(self):
        self.context.invocation_source = SimpleNamespace(kind="MODEL_TOOL_CALL")
        with self.assertRaises(ValueError):
            await self.call(
                "wait_agent", {"agents": [agent_target(random_id("session"))]}
            )
        self.context.invocation_source = SimpleNamespace(kind="CODE_MODE_CALL")
        self.context.session_id = random_id("session")
        with self.assertRaises(ValueError):
            await self.call(
                "wait_agent", {"agents": [agent_target(random_id("session"))]}
            )

    async def test_handoff_uses_internal_active_paging_without_pseudo_agent(self):
        for _ in range(51):
            self.add_work(status="queued")
        first = await self.api.freeze_compaction_handoff()
        second = await self.api.freeze_compaction_handoff(
            cursor=first[-1]["agent"]["agent_id"]
        )
        self.assertEqual(len(first) + len(second), 51)
        self.assertTrue(
            all(
                row["agent"]["agent_id"] in self.repo.agents
                for row in (*first, *second)
            )
        )
