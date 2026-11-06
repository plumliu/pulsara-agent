"""Real common Tool execution distinguishes observation control from Agent mail."""

from __future__ import annotations

import asyncio
import json
from time import monotonic

import pytest
from psycopg.rows import dict_row

from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent, MessageRole, text_part_values
from pulsara_agent.model_input.lowering import decode_tool_result_observation
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.test_round4_plan_host import (
    _DetachedDraftModel,
    _open_test_session,
    _tool_call,
    _text,
)
from tests.test_agent_session_host import eventually

pytestmark = pytest.mark.postgres


class WaitControlModel(_DetachedDraftModel):
    def __init__(self, mode):
        super().__init__()
        self.mode = mode
        self.root_id = None
        self.control = False
        self.steps = {}
        self.selected_work = None
        self.selected_agent = None
        self.other_send = asyncio.Event()
        self.other_message_done = asyncio.Event()
        self.release = asyncio.Event()
        self.other_id = None
        self.control_requests = []
        self.control_cell = None

    async def stream(self, request):
        self.requests.append(request)
        step = self.steps.get(request.turn_id, 0)
        self.steps[request.turn_id] = step + 1
        if request.session_id == self.root_id:
            if not self.control:
                events = (
                    _tool_call(
                        "exec_cell",
                        random_id("call"),
                        {"code": "await tools.run_agent(" + json.dumps({
                            "instruction": "SELECTED_WORK" if step == 0 else "OTHER_WORK"
                        }) + ");"},
                    )
                    if step < 2
                    else _text("ROOT_READY")
                )
            else:
                self.control_requests.append(request)
                if step == 0:
                    operation = "wait_agent.works" if self.mode == "work" else "wait_agent"
                    arguments = {"targets": [self.selected_work if self.mode == "work" else self.selected_agent]}
                    if self.mode == "agent":
                        arguments["timeout_seconds"] = 300
                    code = "const outcome=await tools." + operation + "(" + json.dumps(arguments) + ");"
                    code += ('console.log("UNREACHABLE_AFTER_STOP");' if self.mode == "work"
                             else 'console.log(outcome);')
                    events = _tool_call("exec_cell", random_id("call"), {"code": code})
                elif step == 1:
                    message = next(m for m in reversed(request.compiled_input.messages)
                                   if m.role is MessageRole.TOOL_RESULT)
                    accepted = json.loads(decode_tool_result_observation(text_part_values(message.content)[0])["body"])
                    self.control_cell = accepted["target"]
                    # Keep ROOT active in a real native observation while the Cell
                    # waits; unrelated messages must not complete either wait.
                    events = (_tool_call("manage_cell", random_id("call"),
                        {"action": "wait", "target": self.control_cell, "yield_time_ms": 30_000})
                        if self.mode == "agent" else _text("ROOT_CONTROL_FINISHED"))
                else:
                    events = _text("ROOT_CONTROL_FINISHED")

        else:
            rendered = "\n".join(
                text
                for message in request.compiled_input.messages
                for text in text_part_values(message.content)
            )
            if "OTHER_WORK" in rendered:
                self.other_id = request.session_id
                if step == 0:
                    await self.other_send.wait()
                    events = _tool_call(
                        "send_agent_message",
                        random_id("call"),
                        {
                            "target": {"kind": "AGENT", "agent_id": "ROOT"},
                            "message": "UNSELECTED_AGENT_MESSAGE",
                        },
                    )
                else:
                    self.other_message_done.set()
                    await self.release.wait()
                    events = _text("OTHER_FINAL")
            else:
                await self.release.wait()
                events = _text("SELECTED_FINAL")
        for event in events:
            yield event


@pytest.mark.parametrize("mode", ["work", "agent"])
def test_wait_observers_settle_on_cell_stop_or_natural_completion_after_human_input(
    mode,
    tmp_path,
    monkeypatch,
    stage2_migrated_postgres_database,
):
    async def scenario():
        model = WaitControlModel(mode)
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        core, root = await _open_test_session(
            tmp_path=tmp_path,
            model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
            monkeypatch=monkeypatch,
        )
        model.root_id = root.session_id
        try:
            await asyncio.wait_for(
                root.run_turn(
                    PromptContent.text("start both agents"),
                    requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                ),
                15,
            )

            async def both_running():
                rows = await root._io.run(
                    root.repository.list_query_agent_sessions,
                    root_session_id=root.session_id,
                    observer_session_id=root.session_id,
                    limit=10,
                    deadline_monotonic=monotonic() + 10,
                )
                return (
                    len(rows) == 2
                    and model.other_id is not None
                    and all(row["current_work_id"] for row in rows)
                )

            await eventually(both_running)
            agents = await root._io.run(
                root.repository.list_query_agent_sessions,
                root_session_id=root.session_id,
                observer_session_id=root.session_id,
                limit=10,
                deadline_monotonic=monotonic() + 10,
            )
            selected = next(row for row in agents if row["agent_id"] != model.other_id)
            model.selected_agent = {"kind": "AGENT", "agent_id": selected["agent_id"]}
            model.selected_work = {
                "kind": "WORK",
                "work_id": selected["current_work_id"],
            }
            model.control = True
            control = asyncio.create_task(
                root.run_turn(
                    PromptContent.text("observe selected work"),
                    requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                )
            )

            async def wait_admitted():
                def read(*, deadline_monotonic):
                    with root.repository.connection_provider.connection(
                        lane=PostgresConnectionLane.INSPECTOR,
                        row_factory=dict_row,
                        deadline_monotonic=deadline_monotonic,
                    ) as connection:
                        row = connection.execute(
                            """SELECT a.id FROM pulsara_v3.tool_execution_attempts a
                            JOIN pulsara_v3.tool_invocations i ON i.id=a.invocation_id AND i.session_id=a.session_id
                            LEFT JOIN pulsara_v3.tool_results r ON r.session_id=a.session_id AND r.attempt_id=a.id
                            WHERE a.session_id=%s AND i.source_kind='CODE_MODE_CALL'
                            AND i.tool_name IN ('wait_agent.works','wait_agent') AND r.id IS NULL""",
                            (root.session_id,),
                        ).fetchone()
                        return row is not None

                return await root._io.run(read, deadline_monotonic=monotonic() + 10)

            await eventually(wait_admitted)
            if mode == "work":
                await asyncio.wait_for(control, 10)
                cell = root.code_mode_cells()[0]
                assert cell["pending_calls"] == 1
                owned = root._code_mode._cells[cell["cell_id"]]
                stopped = await root._code_mode.stop(cell["cell_id"])
                assert stopped["state"] == "terminated"

                async def observation_settled():
                    return owned.pending_calls == 0

                await eventually(observation_settled)
                assert owned.state == "terminated" and not owned.output
                from tests.test_agent_code_mode_execution import _formal_cell_rows
                assert not any("UNREACHABLE_AFTER_STOP" in record.get("text", "")
                               for _, record in await asyncio.to_thread(_formal_cell_rows, root))
            else:
                async def root_waiting():
                    return len(model.control_requests) == 2 and model.control_cell is not None
                await eventually(root_waiting)
                model.other_send.set()
                await asyncio.wait_for(model.other_message_done.wait(), 10)
                assert not control.done()
                assert len(model.control_requests) == 2
                outcome = await root.steer_active_turn(
                    command_id=random_id("command"),
                    content=PromptContent.text("HUMAN_STEER_CONTROL"),
                    target_turn_id=root.active_root_turn_id(),
                )
                assert outcome.status in {"PENDING", "SUCCEEDED"}
                assert not control.done()
                assert len(model.control_requests) == 2
                model.release.set()
                await asyncio.wait_for(control, 10)
                assert len(model.control_requests) == 3
                final_input = model.control_requests[-1].compiled_input
                rendered = "\n".join(
                    text
                    for message in final_input.messages
                    for text in text_part_values(message.content)
                )
                assert (
                    "UNSELECTED_AGENT_MESSAGE" in rendered
                    and "HUMAN_STEER_CONTROL" in rendered
                )
                bodies = [
                    decode_tool_result_observation(
                        text_part_values(message.content)[0]
                    )["body"]
                    for message in final_input.messages
                    if message.role is MessageRole.TOOL_RESULT
                ]
                assert any(json.loads(body).get("state") == "completed" for body in bodies)
                from tests.test_agent_code_mode_execution import _formal_cell_rows
                records = [record for _, record in await asyncio.to_thread(_formal_cell_rows, root)]
                assert any(record.get("kind") == "log" and json.loads(record["text"]).get("state") == "settled"
                           for record in records)
            actual = await root._io.run(
                root.repository.query_agent_work,
                root_session_id=root.session_id,
                work_id=model.selected_work["work_id"],
                deadline_monotonic=monotonic() + 10,
            )
            assert actual["status"] == ("running" if mode == "work" else "completed")
        finally:
            model.release.set()
            model.other_send.set()
            await asyncio.wait_for(core.shutdown(), 15)

    asyncio.run(scenario())
