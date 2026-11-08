"""Code Mode waits distinguish a current Work boundary from Agent idle."""

import asyncio
import json
from time import monotonic
from contextlib import asynccontextmanager

import pytest

from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent, MessageRole, text_part_values
from pulsara_agent.model_input.lowering import decode_tool_result_observation
from pulsara_agent.primitives.permission import PermissionMode
from tests.test_agent_session_host import _delegate, _delegation_progress
from tests.test_agent_code_mode_execution import _formal_cell_rows
from tests.test_round4_plan_host import _DetachedDraftModel, _open_test_session, _tool_call, _text

pytestmark = pytest.mark.postgres


class UntilModel(_DetachedDraftModel):
    def __init__(self, until):
        super().__init__()
        self.until = until
        self.root_id = None
        self.agent_id = None
        self.first_turn = None
        self.steps = {}
        self.action = "first"
        self.first_started = asyncio.Event()
        self.second_started = asyncio.Event()
        self.release_first = asyncio.Event()
        self.release_second = asyncio.Event()
        self.cell = None
        self.after_wait = None

    async def stream(self, request):
        self.requests.append(request)
        if request.session_id == self.root_id:
            if progress := _delegation_progress(self, request):
                for event in progress:
                    yield event
                return
            step = self.steps.get(request.turn_id, 0)
            self.steps[request.turn_id] = step + 1
            if self.action in {"first", "second"}:
                if step == 0:
                    parameters = {"instruction": self.action}
                    if self.action == "second":
                        parameters["target"] = {"kind": "AGENT", "agent_id": self.agent_id}
                    events = _delegate(self, random_id("call"), parameters)
                else:
                    events = _text("ASSIGNMENT_ACCEPTED")
            elif step == 0:
                arguments = {"agents": [{"kind": "AGENT", "agent_id": self.agent_id}],
                    "until": self.until, "timeout_seconds": 30}
                events = _tool_call("exec_cell", random_id("call"), {
                    "code": "console.log(await tools.wait_agent(" + json.dumps(arguments) + "));"})
            elif step == 1:
                accepted = next(message for message in reversed(request.compiled_input.messages)
                    if message.role is MessageRole.TOOL_RESULT)
                body = json.loads(decode_tool_result_observation(text_part_values(accepted.content)[0])["body"])
                self.cell = body["target"]
                events = _tool_call("manage_cell", random_id("call"), {
                    "action": "wait", "target": self.cell, "yield_time_ms": 30_000})
            else:
                self.after_wait = request.compiled_input
                events = _text("OBSERVATION_FINISHED")
        else:
            self.agent_id = request.session_id
            if self.first_turn is None:
                self.first_turn = request.turn_id
            if request.turn_id == self.first_turn:
                self.first_started.set()
                await self.release_first.wait()
                events = _text("FIRST_RESULT")
            else:
                self.second_started.set()
                await self.release_second.wait()
                events = _text("SECOND_RESULT")
        for event in events:
            yield event


@pytest.mark.parametrize("until", ["work_end", "idle"])
def test_real_cell_waits_for_current_work_or_queue_drain(
    until, tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    async def scenario():
        model = UntilModel(until)
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        core, root = await _open_test_session(tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        model.root_id = root.session_id
        async def run(action):
            model.action = action
            return await root.run_turn(PromptContent.text(action),
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
        try:
            await asyncio.wait_for(run("first"), 15)
            await asyncio.wait_for(model.first_started.wait(), 15)
            await asyncio.wait_for(run("second"), 15)
            async def work_rows():
                return await root._io.run(root.repository.list_agent_works,
                    root_session_id=root.session_id, agent_id=model.agent_id,
                    limit=10, deadline_monotonic=monotonic() + 10)
            assigned = await work_rows()
            assert [item["status"] for item in assigned] == ["running", "queued"]
            waiting = asyncio.Event()
            original_suspend = core._agent_runtime.suspend_execution
            @asynccontextmanager
            async def observe_suspend(context):
                async with original_suspend(context):
                    if context.session_id == root.session_id:
                        waiting.set()
                    yield
            monkeypatch.setattr(core._agent_runtime, "suspend_execution", observe_suspend)
            observation = asyncio.create_task(run("observe"))
            # Synchronize with the actual wait, after its initial snapshot and
            # binding, rather than with earlier tool admission or advisory UI.
            await asyncio.wait_for(waiting.wait(), 15)
            model.release_first.set()
            await asyncio.wait_for(model.second_started.wait(), 15)
            if until == "idle":
                assert not observation.done()
                assert (await work_rows())[1]["status"] == "running"
                model.release_second.set()
            await asyncio.wait_for(observation, 15)
            records = [record for _, record in await asyncio.to_thread(_formal_cell_rows, root)]
            values = [json.loads(record["text"]) for record in records if record.get("kind") == "log"]
            result = next(value for value in values if value.get("until") == until)
            assert result["state"] == "settled" and result["pending_targets"] == []
            assert not {"bindings", "works"} & result.keys()
            if until == "work_end":
                assert result["observations"] == [{"target": {"kind": "AGENT", "agent_id": model.agent_id},
                    "condition": "work_ended", "work": {"kind": "WORK", "work_id": assigned[0]["work_id"]}}]
                assert (await work_rows())[1]["status"] == "running"
                model.release_second.set()
            else:
                assert result["observations"] == [{"target": {"kind": "AGENT", "agent_id": model.agent_id},
                    "condition": "idle"}]
                assert [item["status"] for item in await work_rows()] == ["completed", "completed"]
                # Work identity is retained by the caller for exact results.
                for work, text in zip(assigned, ("FIRST_RESULT", "SECOND_RESULT"), strict=True):
                    actual = await root._io.run(root.repository.query_agent_work,
                        root_session_id=root.session_id, work_id=work["work_id"],
                        deadline_monotonic=monotonic() + 10)
                    assert actual["final"] == text
        finally:
            model.release_first.set()
            model.release_second.set()
            await core.shutdown()
    asyncio.run(scenario())
