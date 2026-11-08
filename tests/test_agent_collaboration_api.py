"""Observable collaboration contracts, without a second execution engine."""

from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from types import SimpleNamespace
import asyncio
import json
import unittest
from unittest.mock import patch

from pulsara_agent.identity import random_id
from tests.test_agent_collaboration_array_contracts import envelope
from pulsara_agent.primitives.tool_result_budget import ToolResultPageBudget, ToolResultPageBoundary
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import (
    agent_target,
    work_target,
    prepared_request,
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
            ("run_agent", envelope([{"instruction": "valid", "work_title": "中" * 86}])[1]),
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
        args["members"][0]["parameters"]["instruction"] = f"Coordinate with {peers['agents'][0]}"
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
        from pulsara_agent.conversation_kernel._repository.subagents import _SubagentOperations

        agent, work, root = random_id("session"), random_id("queue"), random_id("session")
        class Connection:
            def execute(self, sql, parameters):
                return SimpleNamespace(fetchone=lambda: {"exists": True},
                    fetchall=lambda: [{"id": work, "session_id": agent, "work_title": None}])
        @contextmanager
        def connection(**kwargs):
            yield Connection()
        repository = _SubagentOperations()
        repository._provider = SimpleNamespace(connection=connection)
        repository._agent_work_value = lambda *_: self.fail("inventory must not hydrate a Work body")
        with patch("pulsara_agent.conversation_kernel._repository.subagents.request_execution_from_row",
                   return_value=SimpleNamespace(status=SimpleNamespace(value="completed"), terminal_reason=None)):
            rows = repository.list_query_agent_works(root_session_id=root, observer_session_id=root,
                agent_id=agent, deadline_monotonic=1e20)
        self.assertEqual(rows, ({"work_id": work, "agent_id": agent, "status": "completed",
            "terminal_reason": None, "work_title": None, "cursor": work},))

    def test_message_receipt_rejection_precedes_known_agents_and_prompt_writes(self):
        from datetime import datetime, timezone
        from pulsara_agent.conversation_kernel._repository.subagents import _SubagentOperations

        agent, work, root = random_id("session"), random_id("queue"), random_id("session")
        statements = []
        class Connection:
            def execute(self, sql, parameters):
                statements.append(sql)
                assert sql.startswith("SELECT"), "receipt rejection must precede message SQL effects"
                if sql.startswith("SELECT * FROM pulsara_v3.prompt_queue_items"):
                    row = None
                elif sql.startswith("SELECT coalesce"):
                    row = {"root_id": root}
                elif sql.startswith("SELECT workspace_id"):
                    row = {"workspace_id": random_id("workspace")}
                else:
                    row = {"id": work}
                return SimpleNamespace(fetchone=lambda: row)
        @contextmanager
        def transaction(*args, **kwargs):
            yield Connection()
        repository = _SubagentOperations()
        repository._writer_transaction = transaction
        repository._merge_known_agents = lambda *_args, **_kwargs: self.fail("must not expand known-agent scope before quote")
        def reject(actual_work):
            self.assertEqual(actual_work, work)
            raise ToolResultPageBoundary("no message receipt allowance")
        with self.assertRaises(ToolResultPageBoundary):
            repository.enqueue_agent_steer(SimpleNamespace(session_id=agent), target_turn_id=random_id("turn"),
                queue_item_id=random_id("queue"), source_session_id=root, source_work_id=None,
                text="must not be sent", occurred_at=datetime.now(timezone.utc), actor_id=root,
                deadline_monotonic=1e20, result_check=reject)
        self.assertEqual(len(statements), 5)


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

    def read_agent_work_graph_facts(self, *, root_session_id, work_id, deadline_monotonic):
        row = self.rows.get(work_id)
        return None if row is None else {key: row[key] for key in
            ("work_id", "agent_id", "status", "terminal_reason", "work_title") if key in row}

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
            ui_current_work_title=None if current is None else self.rows[current]["work_title"],
            ui_current_work_status=None if current is None else self.rows[current]["status"],
            ui_queued_work_count=sum(row["status"] == "queued" for row in rows),
        )

    def read_agent_wait_states(self, *, root_session_id, agent_ids, bound_work_ids, deadline_monotonic):
        rows = []
        for identity, work_id in zip(agent_ids, bound_work_ids, strict=True):
            state = self.read_agent_runtime_state(session_id=identity, deadline_monotonic=deadline_monotonic)
            if state is None or state["root_session_id"] != root_session_id:
                continue
            work = self.rows.get(work_id)
            rows.append(dict(agent_id=identity, current_work_id=state["current_work_id"],
                has_running_turn=state["current_work_id"] is not None,
                has_pending_work=state["has_pending_work"],
                bound_work_id=None if work is None else work["work_id"],
                bound_work_status=None if work is None else work["status"]))
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
        self, *, root_session_id, observer_session_id, cursor=None, limit=50,
        active_only=False, deadline_monotonic
    ):
        rows = self._session_rows(
            root_session_id=root_session_id, cursor=cursor,
            limit=len(self.agents) or 1, deadline_monotonic=deadline_monotonic,
        )
        return tuple(
            row for row in rows
            if self._query_visible(observer_session_id, row["agent_id"])
            and (not active_only or row["current_work_id"] is not None or row["queued_count"] > 0)
        )[:limit]

    def list_query_agent_works(self, *, observer_session_id, agent_id, **kwargs):
        if not self._query_visible(observer_session_id, agent_id):
            raise ValueError("AGENT does not exist in this collaboration")
        return self.list_agent_works(agent_id=agent_id, **kwargs)

    def query_visible_agent_work(self, *, observer_session_id, **kwargs):
        row = self.query_agent_work(**kwargs)
        return row if row and self._query_visible(observer_session_id, row["agent_id"]) else None

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
        return await FacadeTests.call(self, name, arguments)

    async def test_poll_and_existing_messages_do_not_yield_a_slot(self):
        self.assertEqual(await self.call("wait_message", {"timeout_seconds": 0}), {"state": "timeout"})
        self.runtime.messages[self.root] = 1
        self.assertEqual(await self.call("wait_message", {}), {"state": "message_available"})
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
        self.assertEqual(await asyncio.wait_for(self.call("wait_message", {"timeout_seconds": 0}), 1),
                         {"state": "timeout"})
        self.runtime.messages[self.root] = 1
        self.assertEqual(await asyncio.wait_for(self.call("wait_message", {}), 1),
                         {"state": "message_available"})
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
        self.assertEqual(await asyncio.wait_for(waiter, 1), {"state": "message_available"})
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
        self.assertEqual(await asyncio.wait_for(self.call("wait_message", {}), 1),
                         {"state": "message_available"})
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
        self.assertEqual(await self.call("wait_message", {}), {"state": "message_available"})

    async def test_unrelated_changes_do_not_extend_deadline(self):
        waiter = asyncio.create_task(self.call("wait_message", {"timeout_seconds": 0.03}))
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
            page_budget=ToolResultPageBudget(tool_call_id=random_id("call"), result_target={
                "kind": "SESSION_ENTRY", "session_id": self.root, "entry_id": random_id("entry")}),
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
        return result.public_value()

    async def test_page_budget_reduces_whole_work_records_and_next_is_directly_callable(self):
        agent, first = self.add_work(status="completed", final="\\\"中" * 40_000)
        _, second = self.add_work(agent=agent, status="completed", final="second final")
        self.context.page_budget = replace(self.context.page_budget,
            accepts_provider_body=lambda body, memory: len(json.loads(body).get("works", [])) <= 1)
        result = await self.api.invoke(tool_name="query_agents", arguments={"target": agent_target(agent)},
            invocation_context=self.context)
        page = result.public_value()
        self.assertTrue(result.page_complete)
        self.assertEqual([item["target"] for item in page["works"]], [work_target(first)])
        for field in ("instruction", "final", "diagnostic"):
            self.assertNotIn(field, page["works"][0])
        following = await self.call(page["next"]["tool"], page["next"]["arguments"])
        self.assertEqual([item["target"] for item in following["works"]], [work_target(second)])
        self.assertIsNone(following["next"])
        self.assertEqual((await self.call("query_agents", page["works"][0]["inspect"]["arguments"]))["final"], "\\\"中" * 40_000)

    async def test_minimum_page_and_admission_budget_failure_make_no_progress_or_work(self):
        agent, _ = self.add_work()
        self.context.page_budget = replace(self.context.page_budget, accepts_provider_body=lambda *_: False)
        with self.assertRaises(ToolResultPageBoundary):
            await self.call("query_agents", {"target": agent_target(agent)})
        before = set(self.repo.rows)
        rejected = await self.api.invoke(tool_name="run_agent",
            arguments=envelope([{"instruction": "must not be admitted"}])[1],
            invocation_context=self.context)
        self.assertEqual(rejected.state, "APPLICATION_ERROR")
        self.assertEqual(rejected.public_value()["error"], "RESULT_BUDGET_EXCEEDED")
        self.assertEqual(set(self.repo.rows), before)
        self.assertEqual(self.runtime.wakes, 0)

    async def test_stop_preserves_entire_set_and_required_facts_without_work_bodies(self):
        agent, running = self.add_work()
        queued = [self.add_work(agent=agent, status="queued")[1] for _ in range(70)]
        self.repo.rows[running]["instruction"] = "large instruction" * 10_000
        result = await self.api.invoke(tool_name="stop_agent", arguments={"target": agent_target(agent)},
            invocation_context=self.context)
        value = result.public_value()
        self.assertEqual({item["target"]["work_id"] for item in value["works"]}, {running, *queued})
        self.assertEqual(result.required_facts["affected_count"], 71)
        self.assertEqual(value["status_counts"], {"stopped": 71})
        for item in value["works"]:
            self.assertNotIn("instruction", item)
            self.assertNotIn("diagnostic", item)
        self.assertTrue(all(self.repo.rows[identity]["status"] == "stopped" for identity in [running, *queued]))

    async def test_wait_modes_do_not_hydrate_or_replay_historical_result_bodies(self):
        agent, _ = self.add_work(status="completed", final="full query result")
        def forbidden(**kwargs):
            raise AssertionError("A wait must not hydrate a complete Work result")
        self.repo.query_agent_work = forbidden
        for until in ("idle", "work_end"):
            waited = await self.call("wait_agent", {"agents": [agent_target(agent)], "until": until})
            self.assertEqual(waited["state"], "settled")
            self.assertEqual(waited["observations"], [{"target": agent_target(agent), "condition": "idle"}])

    async def test_run_receipt_never_leaks_final_from_fast_completed_admission(self):
        original = self.repo.admit_agent_requests
        def completes_before_return(*args, **kwargs):
            rows = original(*args, **kwargs)
            for row in rows:
                row.update(status="completed", final="available only by explicit query")
            return rows
        self.repo.admit_agent_requests = completes_before_return
        accepted = await self.call("run_agent", envelope([{"instruction": "quick task"}])[1])
        self.assertEqual(accepted["details"][0]["status"], "completed")
        self.assertNotIn("final", accepted)
        actual = await self.call("query_agents", {"target": accepted["works"][0]})
        self.assertEqual(actual["final"], "available only by explicit query")

    async def test_batch_uses_one_admission_and_rejection_never_wakes_scheduler(self):
        peers, args = envelope([{"instruction": "a"}, {"instruction": "b"}])
        self.repo.reject = True
        with self.assertRaises(ValueError):
            await self.call("run_agent", args)
        self.assertEqual(self.runtime.wakes, 0)
        self.assertEqual(self.repo.rows, {})
        self.repo.reject = False
        result = await self.call("run_agent", args)
        self.assertEqual(len(self.repo.admissions[-1]), 2)
        self.assertEqual(result["works"][1], peers["works"][1])
        self.assertEqual(self.runtime.wakes, 1)

    async def test_removed_profile_is_rejected_before_single_or_batch_admission(self):
        _, arguments = envelope([{"instruction": "a"}, {"instruction": "b"}])
        members = arguments["members"]
        for value in ("research_worker", None):
            with self.subTest(value=value, operation="single"):
                with self.assertRaisesRegex(ValueError, "unexpected or missing parameter"):
                    await self.call("run_agent", envelope([{"instruction": "x", "profile": value}])[1])
            with self.subTest(value=value, operation="batch"):
                members[1]["parameters"]["profile"] = value
                with self.assertRaisesRegex(ValueError, "unexpected or missing parameter"):
                    await self.call("run_agent", {"members": members})
        self.assertEqual(self.repo.admissions, [])
        self.assertEqual(self.repo.rows, {})
        self.assertEqual(self.runtime.wakes, 0)

    async def test_public_agent_work_wait_and_stop_never_expose_profile(self):
        self.assertNotIn("profile", self.api._public_agent({
            "agent_id": random_id("session"), "profile": "research_worker",
            "ui_current_work_id": None, "ui_current_work_title": None,
            "ui_current_work_status": None, "ui_queued_work_count": 0
        }))
        agent, work = self.add_work(status="completed", final="done")
        self.repo.rows[work]["profile"] = "research_worker"
        self.assertNotIn("profile", await self.call("query_agents", {"target": work_target(work)}))
        listed = await self.call("query_agents", {"target": agent_target(agent)})
        self.assertNotIn("profile", listed["works"][0])
        agents = await self.call("query_agents", {})
        self.assertNotIn("profile", agents["agents"][0])
        waited = await self.call("wait_agent", {"agents": [agent_target(agent)]})
        self.assertNotIn("profile", waited["observations"][0])
        stopped = await self.call("stop_agent", {"target": work_target(work)})
        self.assertNotIn("profile", stopped["works"][0])

    async def test_new_and_reused_agent_work_has_no_profile(self):
        first = await self.call("run_agent", envelope([{"instruction": "first"}])[1])
        second = await self.call("run_agent", envelope([{
            "target": first["agents"][0], "instruction": "continue"
        }])[1])
        self.assertEqual(first["agents"][0], second["agents"][0])
        self.assertNotIn("profile", first)
        self.assertNotIn("profile", second)
        listed = await self.call("query_agents", {"target": first["agents"][0]})
        self.assertEqual(len(listed["works"]), 2)
        self.assertTrue(all("profile" not in work for work in listed["works"]))

    async def test_code_mode_single_and_batch_reject_removed_profile_before_admission(self):
        from pulsara_agent.code_mode import CodeModeHost, CodeModeToolResult
        from tests.test_code_mode_runtime import _finish, _start

        host = CodeModeHost()

        async def invoke(call):
            try:
                value = await self.call(call.tool_name, call.arguments)
            except ValueError as error:
                return CodeModeToolResult(state="FAILED", error_code="invalid_arguments", error_message=str(error))
            return CodeModeToolResult(value=value)

        try:
            page, output = await _finish(host, await _start(host, '''
                for (const profile of ["research_worker", null]) {
                  try { await tools.run_agent([{instruction:"single", profile}]); }
                  catch (error) { console.log({operation:"single", code:error.code, message:error.message}); }
                  try { await tools.run_agent([{instruction:"valid"},
                    {instruction:"invalid", profile}]); }
                  catch (error) { console.log({operation:"batch", code:error.code, message:error.message}); }
                }
            ''', invoke, {"run_agent": {}}))
            self.assertEqual(page.state, "completed")
            import json
            self.assertEqual([json.loads(item["text"]) for item in output], [
                {"operation": operation, "message": "Unknown run_agent assignment field"}
                for _ in range(2)
                for operation in ("single", "batch")
            ])
            self.assertEqual(self.repo.admissions, [])
            self.assertEqual(self.repo.rows, {})
            self.assertEqual(self.runtime.wakes, 0)
        finally:
            await host.aclose()

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
        result = await self.call("run_agent", envelope([{"instruction": "submit exactly once"}])[1])
        self.assertEqual(result["details"][0]["status"], "queued")
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

    async def test_handoff_read_failure_propagates_for_root_and_subagent(self):
        failure = ConversationKernelConflict("active Agent read failed")

        def fail(**kwargs):
            raise failure

        self.repo.list_query_agent_sessions = fail
        with self.assertRaises(ConversationKernelConflict) as caught:
            await self.api.freeze_compaction_handoff()
        self.assertIs(caught.exception, failure)
        self.api.session_id = random_id("session")
        with self.assertRaises(ConversationKernelConflict) as caught:
            await self.api.freeze_compaction_handoff()
        self.assertIs(caught.exception, failure)

    async def test_query_scopes_three_branches_and_keeps_root_alias_out_of_pagination(self):
        own, own_work = self.add_work()
        known, known_work = self.add_work(status="completed", final="known final")
        hidden, hidden_work = self.add_work(status="stopped", final="hidden final")
        self.repo.known[own] = {known}
        root_work = await self.call("query_agents", {"target": work_target(known_work)})
        self.api.session_id = self.context.session_id = own
        self.context.invocation_source = SimpleNamespace(kind="MODEL_TOOL_CALL")
        first = await self.call("query_agents", {"max_items": 1})
        second = await self.call("query_agents", first["next"]["arguments"])
        for page in (first, second):
            self.assertEqual(page["root"], agent_target("ROOT"))
            self.assertEqual(len(page["agents"]), 1)
        self.assertIsNone(second["next"])
        self.assertEqual(
            [page["agents"][0]["agent"]["agent_id"] for page in (first, second)],
            sorted((own, known)),
        )
        for agent, work in ((own, own_work), (known, known_work)):
            page = await self.call("query_agents", {"target": agent_target(agent)})
            self.assertEqual(page["works"][0]["target"], work_target(work))
        self.assertEqual(await self.call("query_agents", {"target": work_target(known_work)}), root_work)
        for target in (agent_target(hidden), work_target(hidden_work), agent_target("ROOT")):
            with self.assertRaises(ValueError):
                await self.call("query_agents", {"target": target})
        self.assertEqual(self.repo.known[own], {known})
        self.assertEqual(await self.api.freeze_compaction_handoff(), ({
            "agent": agent_target(own), "current_work": work_target(own_work), "queued_count": 0,
        },))

    async def test_query_work_pages_keep_target_and_communication_ignores_known_set(self):
        own, _ = self.add_work()
        other, first_work = self.add_work(status="completed", final="first")
        _, second_work = self.add_work(agent=other, status="completed", final="second")
        self.api.session_id = self.context.session_id = own
        with self.assertRaisesRegex(ValueError, "only to ROOT"):
            await self.call("wait_agent", {"agents": [agent_target(other)]})
        self.assertNotIn(own, self.repo.known)
        self.repo.rows[second_work]["status"] = "running"
        sent = await self.call("send_agent_message", {"target": agent_target(other), "message": "hello"})
        self.assertEqual(sent["delivery"], "queued")
        self.repo.known[own] = {other}
        first = await self.call("query_agents", {"target": agent_target(other), "max_items": 1})
        second = await self.call("query_agents", {
            **first["next"]["arguments"],
        })
        self.assertEqual(first["works"][0]["target"], work_target(first_work))
        self.assertEqual(second["works"][0]["target"], work_target(second_work))
        self.assertIsNone(second["next"])
        for name, args in (("run_agent", envelope([{"instruction": "forbidden"}])[1]),
                           ("stop_agent", {"target": agent_target(other)})):
            with self.assertRaisesRegex(ValueError, "only to ROOT"):
                await self.call(name, args)
        self.context.effective_permission_mode = PermissionMode.READ_ONLY
        with self.assertRaisesRegex(ValueError, "bypass permissions"):
            await self.call("query_agents", {})

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
                arguments=envelope([{"instruction": "preserve accepted work"}])[1],
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
        self.assertEqual(result.public_value()["details"][0]["status"], "queued")
        self.assertEqual(len(self.repo.rows), 1)

    async def test_queued_agent_message_and_native_wait_do_not_start_work(self):
        agent, work = self.add_work(status="queued")
        delivery = await self.call(
            "send_agent_message", {"target": agent_target(agent), "message": "question"}
        )
        self.assertEqual(delivery["delivery"], "not_started")
        result = await self.call(
            "wait_agent", {"agents": [agent_target(agent)], "until": "work_end", "timeout_seconds": 0}
        )
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["unavailable_targets"][0]["reason"], "not_started")
        self.assertEqual(self.repo.rows[work]["status"], "queued")
        self.assertEqual(self.runtime.wakes, 0)

    async def test_wait_binds_original_work_and_never_follows_next_request(self):
        agent, original = self.add_work()
        self.repo.rows[original]["profile"] = "research_worker"
        waiter = asyncio.create_task(
            self.call("wait_agent", {"agents": [agent_target(agent)], "until": "work_end"})
        )
        while not self.runtime.waiters:
            await asyncio.sleep(0)
        self.repo.rows[original].update(status="completed", final="original final")
        _, next_work = self.add_work(agent=agent)
        await self.runtime.changed()
        result = await asyncio.wait_for(waiter, 1)
        self.assertEqual(result["state"], "settled")
        self.assertEqual(result["observations"][0]["work"], work_target(original))
        self.assertNotIn("final", result["observations"][0])
        self.assertNotIn("instruction", result["observations"][0])
        self.assertEqual(result["observations"][0]["condition"], "work_ended")
        queried = await self.call("query_agents", {"target": work_target(original)})
        self.assertEqual(queried["final"], "original final")
        self.assertNotIn("profile", result["observations"][0])
        self.assertEqual(self.repo.rows[next_work]["status"], "running")

    async def test_message_wakes_agent_wait_without_copying_body_or_skipping_other_senders(
        self,
    ):
        a, _ = self.add_work()
        b, _ = self.add_work()
        self.runtime.messages[a] = 9
        first = await self.call(
            "wait_agent",
            {"agents": [agent_target(a), agent_target(b)], "timeout_seconds": 0},
        )
        self.assertEqual(first["state"], "message_available")
        self.assertEqual(first["message_sources"], [agent_target(a)])
        self.runtime.messages[b] = 2
        second = await self.call(
            "wait_agent",
            {"agents": [agent_target(a), agent_target(b)], "timeout_seconds": 0},
        )
        self.assertEqual(second["message_sources"], [agent_target(b)])
        third = await self.call(
            "wait_agent",
            {"agents": [agent_target(a), agent_target(b)], "timeout_seconds": 0},
        )
        self.assertEqual(third["state"], "timeout")

    async def test_one_message_wakes_concurrent_observers_without_consumption(self):
        agent, _ = self.add_work()
        arguments = {"agents": [agent_target(agent)], "timeout_seconds": 30}
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
            "wait_agent", {"agents": [agent_target(agent)], "timeout_seconds": 0}
        )
        self.assertEqual(later["state"], "timeout")

    async def test_agent_wait_reports_stopped_bound_work_without_calling_it_success(self):
        agent, work = self.add_work()
        waiter = asyncio.create_task(self.call("wait_agent", {
            "agents": [agent_target(agent)], "until": "work_end"}))
        while not self.runtime.waiters:
            await asyncio.sleep(0)
        self.repo.rows[work].update(status="stopped", terminal_reason="DEPENDENCY_NOT_SUCCESSFUL")
        await self.runtime.changed()
        result = await asyncio.wait_for(waiter, 1)
        self.assertEqual(result["state"], "settled")
        self.assertEqual(result["observations"], [{"target": agent_target(agent),
            "condition": "work_ended", "work": work_target(work)}])
        self.assertEqual((await self.call("query_agents", {"target": work_target(work)}))["status"], "stopped")
        self.assertEqual(self.runtime.suspended, 0)

    async def test_cell_control_interrupts_idle_observation_without_stopping_work(self):
        agent, work = self.add_work(status="queued")
        waiter = asyncio.create_task(self.call("wait_agent", {"agents": [agent_target(agent)]}))
        while not self.runtime.waiters:
            await asyncio.sleep(0)
        self.context.cell_cancel_event.set()
        result = await asyncio.wait_for(waiter, 1)
        self.assertEqual(result["state"], "interrupted")
        self.assertEqual(result["pending_targets"], [agent_target(agent)])
        self.assertEqual(self.repo.rows[work]["status"], "queued")
        self.assertEqual(self.runtime.suspended, 0)

    async def test_unselected_agent_message_does_not_interrupt_but_cell_stop_does(
        self,
    ):
        selected, work = self.add_work()
        other, _ = self.add_work()
        waiter = asyncio.create_task(
            self.call(
                "wait_agent",
                {"agents": [agent_target(selected)], "timeout_seconds": 300},
            )
        )
        while not self.runtime.waiters:
            await asyncio.sleep(0)
        self.runtime.messages[other] = 1
        await self.runtime.changed()
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        self.context.cell_cancel_event.set()
        result = await asyncio.wait_for(waiter, 1)
        self.assertEqual(result["state"], "interrupted")
        self.assertEqual(self.repo.rows[work]["status"], "running")
        self.assertNotIn(other, self.context.agent_message_cursors)

    async def test_code_mode_wait_filters_senders_without_cell_cursors(self):
        selected, _ = self.add_work()
        other, _ = self.add_work()
        self.context.cell_cancel_event = None
        self.context.agent_message_cursors = None
        waiter = asyncio.create_task(self.call(
            "wait_agent", {"agents": [agent_target(selected)], "timeout_seconds": 30}
        ))
        while not self.runtime.waiters:
            await asyncio.sleep(0)
        self.runtime.messages[other] = 1
        await self.runtime.changed()
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        self.runtime.messages[selected] = 1
        await self.runtime.changed()
        result = await asyncio.wait_for(waiter, 1)
        self.assertEqual(result["state"], "message_available")
        self.assertEqual(result["message_sources"], [agent_target(selected)])

    async def test_work_stop_does_not_cancel_unrelated_queued_work(self):
        agent, running = self.add_work()
        _, queued = self.add_work(agent=agent, status="queued")
        _, completed = self.add_work(agent=agent, status="completed", final="kept")
        result = await self.call("stop_agent", {"target": work_target(running)})
        self.assertEqual(result["works"][0]["status"], "stopped")
        self.assertEqual(self.repo.rows[queued]["status"], "queued")
        ended = await self.call("stop_agent", {"target": work_target(completed)})
        self.assertFalse(ended["stopped"])
        self.assertNotIn("final", ended["works"][0])
        self.assertEqual((await self.call("query_agents", ended["works"][0]["inspect"]["arguments"]))["final"], "kept")

    async def test_receipt_arrays_preserve_titles_positions_and_same_agent_fifo(self):
        refs, first_args = envelope([{'instruction': 'create', 'work_title': '旧标题'}])
        first = await self.call('run_agent', first_args)
        self.assertEqual(first['agents'], refs['agents'])
        self.assertEqual(first['works'], refs['works'])
        self.assertEqual(first['details'][0]['work_title'], '旧标题')
        target = first['agents'][0]
        refs, next_args = envelope([
            {'target': target, 'instruction': 'second', 'work_title': '重复标题'},
            {'target': target, 'instruction': 'third', 'work_title': '重复标题'},
            {'target': target, 'instruction': 'fourth'},
        ])
        result = await self.call('run_agent', next_args)
        self.assertEqual(set(result), {'agents', 'works', 'details'})
        self.assertEqual(result['agents'], [target] * 3)
        self.assertEqual(result['works'], refs['works'])
        self.assertEqual([detail['work_title'] for detail in result['details']], ['重复标题', '重复标题', None])
        for index, detail in enumerate(result['details']):
            self.assertEqual(detail['inspect']['arguments']['target'], result['works'][index])
            queried = await self.call('query_agents', detail['inspect']['arguments'])
            self.assertEqual(queried['work_title'], detail['work_title'])
        self.assertEqual(self.repo.rows[first['works'][0]['work_id']]['work_title'], '旧标题')
        self.assertEqual([row['instruction'] for row in self.repo.rows.values()], ['create', 'second', 'third', 'fourth'])

    async def test_root_native_and_child_object_waits_rejected_at_actual_owner(self):
        agent, work = self.add_work(status='completed')
        self.context.invocation_source = SimpleNamespace(kind='MODEL_TOOL_CALL')
        for name, args in [('run_agent', envelope([{'instruction': 'forbidden'}])[1]),
                           ('wait_agent', {'agents': [agent_target(agent)]})]:
            with self.assertRaisesRegex(ValueError, 'live Code Mode'):
                await self.call(name, args)
        with self.assertRaisesRegex(ValueError, 'native subagent'):
            await self.call('wait_message', {})
        self.api.session_id = self.context.session_id = random_id('session')
        for name, args in [('wait_agent', {'agents': [agent_target(agent)]})]:
            with self.assertRaisesRegex(ValueError, 'only to ROOT'):
                await self.call(name, args)
        self.context.invocation_source = SimpleNamespace(kind='CODE_MODE_CALL')
        with self.assertRaisesRegex(ValueError, 'native subagent'):
            await self.call('wait_message', {})
        self.assertEqual(self.repo.admissions, [])

    async def test_root_alias_and_actual_root_target_are_rejected(self):
        for target in (agent_target("ROOT"), agent_target(self.root)):
            with self.assertRaises(ValueError):
                await self.call("wait_agent", {"agents": [target], "timeout_seconds": 0})

    async def test_wait_collection_does_not_inherit_dispatch_member_limit(self):
        agents = [agent_target(self.add_work(status='completed')[0]) for _ in range(17)]
        result = await self.call('wait_agent', {'agents': agents})
        self.assertEqual([item['target'] for item in result['observations']], agents)
        self.assertEqual(result['state'], 'settled')

    async def test_title_receipt_budget_rejects_whole_dispatch_before_admission(self):
        _, args = envelope([{'instruction': 'a', 'work_title': 'final title'}, {'instruction': 'b'}])
        self.context.page_budget = replace(self.context.page_budget, accepts_provider_body=
            lambda body, memory: 'final title' not in body)
        rejected = await self.api.invoke(tool_name='run_agent', arguments=args, invocation_context=self.context)
        self.assertEqual(rejected.state, 'APPLICATION_ERROR')
        self.assertEqual(self.repo.admissions, [])
        self.assertEqual(self.repo.rows, {})
