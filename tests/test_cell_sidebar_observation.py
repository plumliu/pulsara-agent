"""Advisory Cell participants never consume output or block tool work."""

import asyncio
import pytest

from pulsara_agent.code_mode import CodeModeHost, CodeModeToolResult
from pulsara_agent.identity import random_id


async def _start(host):
    async def invoke(_):
        return CodeModeToolResult(value={})
    return await host.start(source="console.log('available');",
        source_exec_attempt_id=random_id("attempt"), turn_id=random_id("turn"),
        snapshot=object(), tool_catalog={}, invoke=invoke)


async def _terminal(host, cell_id):
    async with asyncio.timeout(10):
        while host.capture_cell(cell_id).state not in {"completed", "failed", "terminated"}:
            await asyncio.sleep(.001)


def test_participants_use_one_nonblocking_reservation_and_sticky_incomplete():
    async def run():
        host = CodeModeHost()
        started, release = asyncio.Event(), asyncio.Event()
        async def reserve(_cell, _size):
            started.set()
            await release.wait()
            return True
        host._retain_data = reserve
        try:
            cell_id = await _start(host)
            a, b = random_id("session"), random_id("session")
            host.observe_agent_participation(cell_id, ((a, None, "query"),))
            await started.wait()
            page = host.query_cell_participants(cell_id)
            assert page["participation_coverage"] == "incomplete" and not page["items"]
            host.observe_agent_participation(cell_id, ((a, None, "wait"), (b, None, "stop")))
            release.set()
            async with asyncio.timeout(10):
                while host._cells[cell_id].participation_task is not None:
                    await asyncio.sleep(.001)
            page = host.query_cell_participants(cell_id)
            assert page["participation_coverage"] == "incomplete"
            assert page["items"] == [{"agent_id": a, "work_id": None, "kinds": ["query", "wait"]}]
            await _terminal(host, cell_id)
            assert host.query_cell_participants(cell_id) == page
            output = await host.observe(cell_id, 0)
            assert output.output == ({"kind": "log", "text": "available"},)
            host.abandon(output)
        finally:
            release.set()
            await host.aclose()
    asyncio.run(run())


def test_real_participant_reservation_survives_terminal_until_cell_retirement():
    async def run():
        host = CodeModeHost()
        try:
            cell_id = await _start(host)
            agent, work = random_id("session"), random_id("queue")
            host.observe_agent_participation(cell_id, ((agent, work, "wait"),))
            async with asyncio.timeout(10):
                while host._cells[cell_id].participation_task is not None:
                    await asyncio.sleep(.001)
            assert host.query_cell_participants(cell_id) == {
                "participation_coverage": "complete",
                "items": [{"agent_id": agent, "work_id": work, "kinds": ["wait"]}],
                "next_cursor": None,
            }
            await _terminal(host, cell_id)
            page = await host.observe(cell_id, 0)
            await host._cells[cell_id].vm_released.wait()
            await host.acknowledge(page)
            assert cell_id not in host._cells
        finally:
            await host.aclose()
    asyncio.run(run())


def test_collaboration_observes_only_validated_targets_before_wait_or_message_result():
    from types import SimpleNamespace
    from pulsara_agent.conversation_kernel.io import KernelSessionIO
    from pulsara_agent.conversation_kernel.subagents.collaboration_api import CollaborationToolPort
    from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import agent_target
    from pulsara_agent.primitives.permission import PermissionMode
    from tests.test_agent_collaboration_api import RepositoryOwner, RuntimeOwner

    async def run():
        root, agent, work = random_id("session"), random_id("session"), random_id("queue")
        repo, seen = RepositoryOwner(root), []
        repo.agents.add(agent)
        repo.rows[work] = {"agent_id": agent, "work_id": work, "instruction": "work",
                           "status": "running", "terminal_reason": None}
        runtime = RuntimeOwner(repo)
        port = CollaborationToolPort(repository=repo, root_session_id=root, session_id=root,
            runtime=runtime, admission_facts=lambda *_: {}, io=KernelSessionIO())
        class Observer:
            def observe_agent_participation(self, cell_id, refs):
                seen.extend(refs)
            def mark_cell_participation_incomplete(self, cell_id):
                raise AssertionError("unexpected observation failure")
        port.bind_cell_observer(Observer())
        context = SimpleNamespace(session_id=root, code_cell_id=random_id("cell"),
            effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            agent_message_cursors={}, cell_cancel_event=asyncio.Event())
        await port.invoke(tool_name="query_agents", arguments={}, invocation_context=context)
        assert seen == []
        with pytest.raises(ValueError):
            await port.invoke(tool_name="query_agents", arguments={"target": agent_target(random_id("session"))}, invocation_context=context)
        assert seen == []
        task = asyncio.create_task(port.invoke(tool_name="wait_agent",
            arguments={"targets": [agent_target(agent)], "timeout_seconds": 30}, invocation_context=context))
        async with asyncio.timeout(5):
            while runtime.waiters == 0:
                await asyncio.sleep(.001)
        assert not task.done() and seen == [(agent, work, "wait")]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async def fail_after_effect(**_):
            raise OSError("delivery failed after admission")
        runtime.send_agent_steer = fail_after_effect
        with pytest.raises(OSError):
            await port.invoke(tool_name="send_agent_message",
                arguments={"target": agent_target(agent), "message": "message"}, invocation_context=context)
        assert seen[-1] == (agent, None, "message")
    asyncio.run(run())


def test_real_budget_rejection_marks_incomplete_without_failing_cell():
    async def run():
        host = CodeModeHost()
        try:
            cell_id = await _start(host)
            await _terminal(host, cell_id)
            cell = host._cells[cell_id]
            await cell.vm_released.wait()
            low, high = 0, host.limits.cell_bytes
            while low < high:
                amount = (low + high + 1) // 2
                if await host._retain_data(cell, amount):
                    await host._send({"type": "release", "cell_id": cell_id, "bytes": amount})
                    low = amount
                else:
                    high = amount - 1
            assert low > 0 and await host._retain_data(cell, low)
            host.observe_agent_participation(cell_id, ((random_id("session"), None, "query"),))
            async with asyncio.timeout(10):
                while cell.participation_task is not None:
                    await asyncio.sleep(.001)
            assert host.query_cell_participants(cell_id)["participation_coverage"] == "incomplete"
            assert host.capture_cell(cell_id).state == "completed"
            await host._send({"type": "release", "cell_id": cell_id, "bytes": low})
            page = await host.observe(cell_id, 0)
            assert page.output == ({"kind": "log", "text": "available"},)
            await host.acknowledge(page)
        finally:
            await host.aclose()
    asyncio.run(run())


def test_real_accepted_reservation_does_not_hold_retiring_cell():
    async def run():
        host = CodeModeHost()
        accepted, release = asyncio.Event(), asyncio.Event()
        original = host._retain_data
        async def delay_after_ack(cell, amount):
            result = await original(cell, amount)
            assert result
            accepted.set()
            await release.wait()
            return result
        host._retain_data = delay_after_ack
        try:
            cell_id = await _start(host)
            await _terminal(host, cell_id)
            await host._cells[cell_id].vm_released.wait()
            host.observe_agent_participation(cell_id, ((random_id("session"), None, "query"),))
            await asyncio.wait_for(accepted.wait(), 10)
            task = host._cells[cell_id].participation_task
            page = await host.observe(cell_id, 0)
            await host.acknowledge(page)
            assert cell_id not in host._cells
            await asyncio.gather(task, return_exceptions=True)
            replacement = await _start(host)
            await _terminal(host, replacement)
            assert host.capture_cell(replacement).state == "completed"
        finally:
            release.set()
            await host.aclose()
    asyncio.run(run())


@pytest.mark.postgres
def test_actual_cell_work_graph_pages_use_canonical_source_and_root(tmp_path, monkeypatch, stage2_migrated_postgres_database):
    from pulsara_agent.llm.input import PromptContent
    from pulsara_agent.primitives.permission import PermissionMode
    from tests.support.round3 import CallbackScriptedKernelModel
    from tests.test_round4_plan_host import _open_test_session, _text, _tool_call

    async def run():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        root_id = None
        release = asyncio.Event()
        root_calls = 0
        async def stream(request):
            nonlocal root_calls
            if request.session_id != root_id:
                await release.wait()
                events = _text("child finished")
            else:
                root_calls += 1
                if root_calls == 1:
                    code = ("const x=await tools.run_agent.batch({a:{instruction:'first'},"
                            "b:{instruction:'second'}}); "
                            "await tools.wait_agent.works({targets:[x.a.work,x.b.work]});")
                    events = _tool_call("exec_cell", random_id("call"), {"code": code, "yield_time_ms": 0})
                else:
                    events = _text("root done")
            for event in events:
                yield event
        model = CallbackScriptedKernelModel(stream)
        core, root = await _open_test_session(tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        root_id = root.session_id
        try:
            result = await root.run_turn(PromptContent.text("start batch"),
                command_id=random_id("command"), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            assert result.final_text == "root done"
            async with asyncio.timeout(20):
                while True:
                    cells = root._code_mode.list_cell_snapshots()["cells"]
                    if cells:
                        cell_id = cells[0]["target"]["cell_id"]
                        first = await root._tools.read_code_cell_graph(cell_id=cell_id, section="works", max_items=1)
                        if first["items"] and first["next_cursor"]:
                            break
                    await asyncio.sleep(.01)
            second = await root._tools.read_code_cell_graph(cell_id=cell_id, section="works",
                cursor=first["next_cursor"], max_items=1)
            assert len(second["items"]) == 1 and second["next_cursor"] is None
            actual = [first["items"][0]["work_id"], second["items"][0]["work_id"]]
            assert actual == sorted(actual) and len(set(actual)) == 2
            snap = root._tools.capture_code_cell(cell_id)
            repo = root.repository
            deadline = root._collaboration.runtime.read_deadline()
            foreign = await root._io.run(repo.list_cell_agent_works, root_session_id=random_id("session"),
                source_exec_attempt_id=snap.source_exec_attempt_id, limit=10, deadline_monotonic=deadline)
            other = await root._io.run(repo.list_cell_agent_works, root_session_id=root_id,
                source_exec_attempt_id=random_id("attempt"), limit=10, deadline_monotonic=deadline)
            assert foreign == other == ()
        finally:
            release.set()
            await core.shutdown()
    asyncio.run(run())



def test_late_accepted_retention_ack_after_forget_does_not_release_twice():
    async def run():
        host = CodeModeHost()
        ack_received = asyncio.Event()
        delayed = []
        original_complete, original_send = host._complete_retention, host._send
        sent = []
        def defer_ack(cell, message):
            delayed.append((cell, dict(message)))
            ack_received.set()
        async def record_send(message):
            sent.append(dict(message))
            await original_send(message)
        host._complete_retention = defer_ack
        host._send = record_send
        try:
            cell_id = await _start(host)
            await _terminal(host, cell_id)
            await host._cells[cell_id].vm_released.wait()
            host.observe_agent_participation(cell_id, ((random_id("session"), None, "query"),))
            await asyncio.wait_for(ack_received.wait(), 10)
            assert delayed[0][1]["accepted"] is True
            task = host._cells[cell_id].participation_task
            page = await host.observe(cell_id, 0)
            await host.acknowledge(page)
            await asyncio.gather(task, return_exceptions=True)
            assert cell_id not in host._cells
            assert sum(message["type"] == "forget" and message["cell_id"] == cell_id for message in sent) == 1
            before = list(sent)
            original_complete(*delayed[0])
            await asyncio.sleep(0)
            assert sent == before
            replacement = await _start(host)
            await _terminal(host, replacement)
            assert host.capture_cell(replacement).state == "completed"
        finally:
            await host.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("code_first", [False, True])
def test_cell_observer_is_bound_for_both_composition_orders(tmp_path, code_first):
    from types import SimpleNamespace
    from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
    from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
    from pulsara_agent.conversation_kernel.tool_policy import DefaultToolDispatchAuthorizationPolicy

    async def run():
        seen = []
        port = DirectKernelToolPort(workspace_root=tmp_path, host_owner_id="host",
            session_id=random_id("session"), live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy())
        code = CodeModeHost()
        subagent = SimpleNamespace(bind_cell_observer=seen.append)
        try:
            if code_first:
                port.bind_code_mode_port(code)
                port.bind_subagent_port(subagent)
            else:
                port.bind_subagent_port(subagent)
                port.bind_code_mode_port(code)
            assert seen == [code]
        finally:
            await port.aclose()
            await code.aclose()
    asyncio.run(run())


def test_cell_observer_missing_from_bound_subagent_is_not_silently_ignored(tmp_path):
    from types import SimpleNamespace
    from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
    from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
    from pulsara_agent.conversation_kernel.tool_policy import DefaultToolDispatchAuthorizationPolicy

    async def run():
        port = DirectKernelToolPort(workspace_root=tmp_path, host_owner_id="host",
            session_id=random_id("session"), live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy())
        code = CodeModeHost()
        try:
            port.bind_subagent_port(SimpleNamespace())
            with pytest.raises(AttributeError, match="bind_cell_observer"):
                port.bind_code_mode_port(code)
        finally:
            await port.aclose()
            await code.aclose()
    asyncio.run(run())
