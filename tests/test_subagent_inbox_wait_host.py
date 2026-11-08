"""Native inbox waiting through real Host, queue, scheduler and provider input."""

import asyncio
from time import monotonic
from types import SimpleNamespace

import pytest
from psycopg.rows import dict_row

from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent, text_part_values
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.test_agent_session_host import _delegate, _delegation_progress, eventually
from tests.test_round4_plan_host import _DetachedDraftModel, _open_test_session, _text, _tool_call

pytestmark = pytest.mark.postgres


class InboxModel(_DetachedDraftModel):
    def __init__(self):
        super().__init__()
        self.root_id = None
        self.child_id = None
        self.first_turn = None
        self.action = "start"
        self.steps = {}
        self.resumed = asyncio.Event()
        self.finish = asyncio.Event()
        self.followup = asyncio.Event()
        self.resumed_input = None

    async def stream(self, request):
        self.requests.append(request)
        step = self.steps.get(request.turn_id, 0)
        if request.session_id == self.root_id:
            if progress := _delegation_progress(self, request):
                for event in progress:
                    yield event
                return
            self.steps[request.turn_id] = step + 1
            if step:
                events = _text("ROOT_FINISHED")
            elif self.action in {"start", "queue", "peer"}:
                parameters = {"instruction": "WAIT_FIRST" if self.action == "start" else self.action}
                if self.action == "queue":
                    parameters["target"] = {"kind": "AGENT", "agent_id": self.child_id}
                events = _delegate(self, random_id("call"), parameters)
            else:
                name = "stop_agent" if self.action == "stop" else "send_agent_message"
                arguments = {"target": {"kind": "AGENT", "agent_id": self.child_id}}
                if name == "send_agent_message":
                    arguments["message"] = "ROOT_REPLY_BODY"
                events = _tool_call(name, random_id("call"), arguments)
        else:
            self.steps[request.turn_id] = step + 1
            if self.child_id is None:
                self.child_id = request.session_id
                self.first_turn = request.turn_id
            if request.session_id != self.child_id:
                events = (_tool_call("send_agent_message", random_id("call"), {
                    "target": {"kind": "AGENT", "agent_id": self.child_id},
                    "message": "PEER_REPLY_BODY",
                }) if step == 0 else _text("PEER_FINISHED"))
            elif request.turn_id != self.first_turn:
                self.followup.set()
                events = _text("FOLLOWUP_FINISHED")
            elif step == 0:
                events = _tool_call("wait_message", random_id("call"), {})
            else:
                self.resumed_input = request.compiled_input
                self.resumed.set()
                await self.finish.wait()
                events = _text("CHILD_RESULT_WITH_EVIDENCE")
        for event in events:
            yield event


async def run_root(root, model, action):
    model.action = action
    return await asyncio.wait_for(root.run_turn(
        PromptContent.text(action), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
    ), 15)


async def wait_pending(root, model):
    def read():
        with root.repository.connection_provider.connection(
            lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
            deadline_monotonic=monotonic() + 10,
        ) as connection:
            return connection.execute("""SELECT a.id FROM pulsara_v3.tool_execution_attempts a
                JOIN pulsara_v3.tool_invocations i ON i.id=a.invocation_id AND i.session_id=a.session_id
                JOIN pulsara_v3.assistant_message_blocks b ON b.session_id=i.session_id AND b.tool_call_id=i.tool_call_id
                LEFT JOIN pulsara_v3.tool_results r ON r.session_id=a.session_id AND r.attempt_id=a.id
                WHERE a.session_id=%s AND b.tool_name='wait_message' AND r.id IS NULL""",
                (model.child_id,),
            ).fetchone() is not None
    await eventually(lambda: asyncio.to_thread(read))


@pytest.mark.parametrize("sender", ["message", "peer"])
def test_inbox_wait_wakes_on_any_sender_but_not_queued_work(
    sender, tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    async def scenario():
        model = InboxModel()
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        core, root = await _open_test_session(tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        model.root_id = root.session_id
        await core._agent_runtime.set_capacity_target(1)
        try:
            await run_root(root, model, "start")
            await wait_pending(root, model)
            async def slot_released():
                return (await core._agent_runtime.capacity_state())[1] == 0
            await eventually(slot_released)
            await run_root(root, model, "queue")
            assert not model.resumed.is_set() and not model.followup.is_set()
            await run_root(root, model, sender)
            await asyncio.wait_for(model.resumed.wait(), 15)
            rendered = "\n".join(text for message in model.resumed_input.messages
                for text in text_part_values(message.content))
            assert ("ROOT_REPLY_BODY" if sender == "message" else "PEER_REPLY_BODY") in rendered
            first = next(request.compiled_input for request in model.requests
                if request.turn_id == model.first_turn)
            assert model.resumed_input.system_prompt == first.system_prompt
            assert model.resumed_input.tools == first.tools
            assert model.resumed_input.messages[:len(first.messages)] == first.messages
            assert not model.followup.is_set()
            # The real consumed canonical entry remains visible before the
            # installed frontier, then ceases to be unread at that frontier.
            child = core._live_agent_session(model.child_id)
            assert child is not None
            works = await child._io.run(child.repository.list_agent_works,
                root_session_id=root.session_id, agent_id=model.child_id, limit=10,
                deadline_monotonic=monotonic() + 10)
            current_work = next(work for work in works if work["status"] == "running")
            snapshot = await child._io.run(child.repository.read_agent_wait_states,
                root_session_id=root.session_id, agent_ids=(model.child_id,),
                bound_work_ids=(None,), deadline_monotonic=monotonic() + 10)
            assert snapshot[0]["current_work_id"] == current_work["work_id"]
            async def available(through):
                return await child._io.run(child.repository.has_pending_agent_message,
                session_id=model.child_id, turn_id=model.first_turn,
                accepted_through_sequence=through, deadline_monotonic=monotonic() + 10)
            assert await available(0)
            assert not await core._agent_runtime.has_pending_agent_message(
                invocation_context=SimpleNamespace(session_id=model.child_id, turn_id=model.first_turn))
            assert not await child._io.run(child.repository.has_pending_agent_message,
                session_id=model.child_id, turn_id=random_id("turn"),
                accepted_through_sequence=0, deadline_monotonic=monotonic() + 10)
            assert not await root._io.run(root.repository.has_pending_agent_message,
                session_id=root.session_id, turn_id=model.first_turn,
                accepted_through_sequence=0, deadline_monotonic=monotonic() + 10)
            model.finish.set()
            await asyncio.wait_for(model.followup.wait(), 15)
        finally:
            model.finish.set()
            await core.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("control", ["stop", "close"])
def test_native_wait_can_be_stopped_or_closed_without_leaking_a_slot(
    control, tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    async def scenario():
        model = InboxModel()
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        core, root = await _open_test_session(tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        model.root_id = root.session_id
        try:
            await run_root(root, model, "start")
            await wait_pending(root, model)
            if control == "stop":
                await run_root(root, model, "stop")
            else:
                child = core._live_agent_session(model.child_id)
                await asyncio.wait_for(core.close_session(child.host_session_id), 15)
            async def stopped():
                row = await root._io.run(root.repository.read_agent_runtime_state,
                    session_id=model.child_id, deadline_monotonic=monotonic() + 10)
                return row["current_work_id"] is None
            await eventually(stopped)
            assert (await core._agent_runtime.capacity_state())[1] == 0
            assert not model.resumed.is_set()
        finally:
            model.finish.set()
            await core.shutdown()
    asyncio.run(scenario())
