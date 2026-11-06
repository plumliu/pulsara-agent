"""A real Host Stop joins the late tool result before cancelling its batch."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from threading import Event
from time import monotonic

import pytest
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel import io as io_module
from pulsara_agent.conversation_kernel.execution_watchdogs import KernelExecutionDeadlineFactory
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.tools.builtins.filesystem import ReadFileTool, WriteFileTool
from tests.test_round4_plan_host import _DetachedDraftModel, _open_test_session, _tool_call


pytestmark = pytest.mark.postgres


def test_stop_after_tool_watchdog_settles_once_and_never_dispatches_batch_sibling(
    tmp_path, monkeypatch, isolated_migrated_postgres_database,
):
    source = tmp_path / "source.txt"
    source.write_text("exact late read result", encoding="utf-8")
    marker = tmp_path / "must-not-exist.txt"
    first_call, second_call = random_id("call"), random_id("call")
    entered, release = Event(), Event()
    physical_calls = []
    original_read = ReadFileTool.execute
    original_write = WriteFileTool.execute

    def blocked_read(tool, call):
        physical_calls.append(call.id)
        entered.set()
        assert release.wait(20), "test did not release the physical read"
        return original_read(tool, call)

    def observed_write(tool, call, **kwargs):
        physical_calls.append(call.id)
        return original_write(tool, call, **kwargs)

    monkeypatch.setattr(ReadFileTool, "execute", blocked_read)
    monkeypatch.setattr(WriteFileTool, "execute", observed_write)

    class Model(_DetachedDraftModel):
        async def stream(self, request):
            self.requests.append(request)
            assert len(self.requests) == 1, "Stop must prevent the next provider call"
            for event in (
                *_tool_call("read_file", first_call, {"target": {"kind": "PATH", "path": source.name}}),
                *_tool_call("write_file", second_call, {
                    "target": {"kind": "PATH", "path": marker.name}, "content": "must not dispatch",
                }),
            ):
                yield event

    async def scenario():
        model = Model()
        core, session = await _open_test_session(
            tmp_path=tmp_path, model=model,
            postgres_dsn=isolated_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch,
        )
        draining = asyncio.Event()
        original_drain = io_module._drain_physical_task

        async def observed_drain(task, **kwargs):
            if entered.is_set() and not release.is_set():
                draining.set()
            return await original_drain(task, **kwargs)

        monkeypatch.setattr(io_module, "_drain_physical_task", observed_drain)
        session._tools._deadlines = KernelExecutionDeadlineFactory(replace(
            session._tools._deadlines.policy, nonterminal_tool_attempt_seconds=.02,
        ))
        running = asyncio.create_task(session.run_turn(
            PromptContent.text("Read source.txt and then write must-not-exist.txt."),
            command_id=random_id("command"), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
        ))
        try:
            await asyncio.wait_for(draining.wait(), 10)
            turn_id = session._active_turn_id
            command_id = random_id("command")
            accepted = await session.request_stop_turn(
                command_id=command_id, target_turn_id=turn_id,
                admission_deadline_ms=session.control_admission_deadline_ms(),
                expected_session_id=session.session_id, expected_host_session_id=session.host_session_id,
            )
            assert accepted.status == "PENDING"
            assert not running.done()
            assert physical_calls == [first_call]
            release.set()
            await asyncio.wait_for(asyncio.gather(running, return_exceptions=True), 10)
            attempt = session._user_control_attempts[command_id]
            await asyncio.wait_for(attempt.task, 10)
            outcome = (await session.query_control_command(attempt.request)).outcome
            assert outcome.status == "SUCCEEDED"
            assert outcome.public_code == "ROOT_STOPPED"
            assert outcome.user_control.root.status == "INTERRUPTED"
            assert physical_calls == [first_call]
            assert not marker.exists()
            assert len(model.requests) == 1
            with session.repository.connection_provider.connection(
                lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
                deadline_monotonic=monotonic() + 10,
            ) as connection:
                results = connection.execute(
                    "SELECT result_state, result_origin_kind FROM pulsara_v3.tool_results "
                    "WHERE session_id=%s AND result_origin_kind='PHYSICAL_ATTEMPT'",
                    (session.session_id,),
                ).fetchall()
            assert results == [{"result_state": "SUCCESS", "result_origin_kind": "PHYSICAL_ATTEMPT"}]
        finally:
            release.set()
            await asyncio.gather(running, return_exceptions=True)
            await core.close_session(session.host_session_id)
            await core.shutdown()

    asyncio.run(scenario())
