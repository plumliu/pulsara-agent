"""Real model query dispatch leaves process and Cell output owners untouched."""

import asyncio
import inspect
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pulsara_agent.code_mode import CodeModeHost, CodeModeToolResult
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.tool_policy import (
    DefaultToolDispatchAuthorizationPolicy,
)
from pulsara_agent.identity import random_id
from tests.support.model_config import scheduled_task_service
from tests.support.round3 import direct_tool_invocation_context


def runtime(tmp_path, host=None, read_source=None, *, save_outcome=None, read_outcome=None):
    port = DirectKernelToolPort(
        scheduled_tasks=scheduled_task_service(),
        workspace_root=tmp_path,
        host_owner_id=random_id("host"),
        session_id=random_id("session"),
        authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        live_bus=LiveAgentEventBus(),
    )
    if host is not None:
        port.bind_subagent_port(
            SimpleNamespace(tool_names=(), bind_cell_observer=lambda observer: None)
        )
        port.bind_code_mode_port(host)
        port.bind_code_mode_executor(
            invoke=None, freeze_source=None, read_source=read_source,
            save_outcome=save_outcome, read_outcome=read_outcome,
        )
    return port


async def dispatch(port, name, arguments, *, accept=True):
    call, attempt, turn, entry = (
        random_id("call"),
        random_id("attempt"),
        random_id("turn"),
        random_id("entry"),
    )
    borrow, context = direct_tool_invocation_context(
        port,
        session_id=port._session_id,
        tool_name=name,
        tool_call_id=call,
        attempt_id=attempt,
        turn_id=turn,
        assistant_entry_id=entry,
    )
    result = None
    try:
        result = await port.invoke(
            tool_name=name,
            arguments=arguments,
            tool_call_id=call,
            attempt_id=attempt,
            turn_id=turn,
            assistant_entry_id=entry,
            invocation_context=context,
        )
        if name in {"exec_process", "manage_process", "query_processes"}:
            from pulsara_agent.ports.terminal import terminal_result_schema
            from tests.support.tool_result_schemas import validate_business_result

            validate_business_result(terminal_result_schema(name), result.content, result.state)
        if accept and result.acceptance_succeeded is not None:
            returned = await asyncio.to_thread(result.acceptance_succeeded)
            if inspect.isawaitable(returned):
                await returned
        return result
    finally:
        if result is not None and result.acceptance_finished is not None:
            result.acceptance_finished()
        borrow.close()


def test_complete_cell_source_query_does_not_take_output_owner(tmp_path):
    async def run():
        host = CodeModeHost()
        source = "console.log('retained output');\n/*" + "冻结🙂\n" * 15000 + "*/"
        attempt = random_id("attempt")
        reads = []

        async def read_source(**values):
            reads.append(values)
            return source

        async def no_tools(_):
            return CodeModeToolResult(value={})

        saved = AsyncMock()
        archived = AsyncMock(return_value=(attempt, "completed", None))
        port = runtime(tmp_path, host, read_source, save_outcome=saved, read_outcome=archived)
        try:
            cell = await host.start(
                source=source,
                source_exec_attempt_id=attempt,
                turn_id=random_id("turn"),
                snapshot=object(),
                tool_catalog={"decide": {"parameters": {}}},
                invoke=no_tools,
                decision_available=False,
            )
            await host.wait_state(cell, 30000)
            lease = await host.lease_output(cell)
            assert lease.record == {
                "kind": "log",
                "values": [{"type": "text", "value": "retained output"}],
            }
            target = {"kind": "CELL", "cell_id": cell}
            inventory = await dispatch(port, "query_cells", {})
            assert inventory.content.content[0].value["target"] == target
            detail = await dispatch(port, "query_cells", {"target": target})
            assert detail.state == "SUCCESS" and detail.process_local_settlement is None
            assert detail.content.content[0].value == source
            assert reads == [{"session_id": port._session_id, "attempt_id": attempt}]
            assert await host.lease_output(cell) is lease
            assert (await host.wait_state(cell, 0))["state"] == "completed"
            assert await host.lease_output(cell) is lease
            await host.acknowledge_output(lease)
            async with asyncio.timeout(5):
                while host.snapshot_cell_index():
                    await asyncio.sleep(0.001)
            inventory = await dispatch(port, "query_cells", {})
            assert [part.type for part in inventory.content.content] == ["text"]
            historical = await dispatch(port, "query_cells", {"target": target})
            assert historical.public_value() == {
                "target": target,
                "meta": {"status": "SUCCESS", "cell_status": "completed", "pending_calls": 0},
                "content": [{"type": "text", "value": source}],
            }
            saved.assert_awaited_once_with(attempt, "completed", None)
            archived.assert_awaited_once_with(session_id=port._session_id, cell_id=cell)
            unavailable = await dispatch(port, "manage_cell", {"target": target, "action": "wait"})
            assert unavailable.state == "APPLICATION_ERROR"
        finally:
            await host.aclose()
            await port.aclose()

    asyncio.run(run())


@pytest.mark.postgres
def test_native_query_reads_a_reclaimed_cell_after_resuming_its_session(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    from psycopg.rows import dict_row

    from pulsara_agent.llm.input import PromptContent
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.round3 import CallbackScriptedKernelModel
    from tests.test_round4_plan_host import _open_test_session, _text, _tool_call

    script = "// A silent Cell also has a queryable execution record.\nconst answer = 42;\n"
    target = None

    async def stream(_request):
        if len(model.requests) == 1:
            events = _tool_call("exec_cell", random_id("call"), {"code": script})
        elif len(model.requests) == 3:
            events = _tool_call("query_cells", random_id("call"), {"target": target})
        else:
            events = _text("done")
        for event in events:
            yield event

    async def run():
        nonlocal target
        core, session = await _open_test_session(
            tmp_path=tmp_path, monkeypatch=monkeypatch, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
        )
        try:
            await session.run_turn(PromptContent.text("execute the script"),
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            async with asyncio.timeout(10):
                while session._code_mode.snapshot_cell_index():
                    await asyncio.sleep(.01)
            with session.repository.connection_provider.connection(
                lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
                deadline_monotonic=monotonic()+5,
            ) as connection:
                row = connection.execute(
                    """SELECT public_fields->'target' AS target FROM pulsara_v3.tool_results
                       WHERE session_id=%s AND public_fields->'target'->>'kind'='CELL'""",
                    (session.session_id,),
                ).fetchone()
                target = row["target"]
            workspace = HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path)
            other = await core.open_session(workspace)
            foreign = await dispatch(other._tools, "query_cells", {"target": target})
            assert foreign.public_value()["meta"] == {"status": "ERROR"}
            await core.close_session(session.host_session_id)
            resumed = await core.resume_session(session.session_id, workspace_input=workspace)
            assert not resumed._code_mode.snapshot_cell_index()
            await resumed.run_turn(PromptContent.text("query it"),
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            with resumed.repository.connection_provider.connection(
                lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
                deadline_monotonic=monotonic()+5,
            ) as connection:
                row = connection.execute(
                    """SELECT id FROM pulsara_v3.tool_results WHERE session_id=%s
                       ORDER BY accepted_at DESC LIMIT 1""",
                    (resumed.session_id,),
                ).fetchone()
            stored = resumed.repository.read_accepted_tool_result_observation(
                session_id=resumed.session_id, result_id=row["id"], deadline_monotonic=monotonic()+5,
            )
            assert stored["value"] == {
                "target": target,
                "meta": {"status": "SUCCESS", "cell_status": "completed", "pending_calls": 0},
                "content": [{"type": "text", "value": script}],
            }
        finally:
            await core.shutdown()

    model = CallbackScriptedKernelModel(stream)
    asyncio.run(run())


def test_source_failure_keeps_live_cell_and_stop_does_not_read_output(tmp_path):
    async def run():
        host = CodeModeHost()
        started, finish = asyncio.Event(), asyncio.Event()

        async def invoke(_):
            started.set()
            await finish.wait()
            return CodeModeToolResult(value={})

        async def read_source(**values):
            raise ValueError("canonical content is corrupt")

        port = runtime(tmp_path, host, read_source)
        try:
            cell = await host.start(
                source="console.log('keep');await tools.read_file({});",
                source_exec_attempt_id=random_id("attempt"),
                turn_id=random_id("turn"),
                snapshot=object(),
                tool_catalog={
                    name: {"parameters": {}} for name in ("decide", "read_file")
                },
                invoke=invoke,
                decision_available=False,
            )
            await asyncio.wait_for(started.wait(), 5)
            target = {"kind": "CELL", "cell_id": cell}
            result = await dispatch(port, "query_cells", {"target": target})
            assert (
                result.state == "APPLICATION_ERROR"
                and result.content.content[0].value == "canonical content is corrupt"
            )
            assert host.capture_cell(cell).pending_calls == 1
            inventory = await dispatch(port, "query_cells", {})
            assert inventory.content.content[0].value["pending_calls"] == 1
            stopped = await dispatch(
                port, "manage_cell", {"action": "stop", "target": target}
            )
            assert (
                stopped.state == "SUCCESS"
                and stopped.content.public_fields["target"] == target
            )
            assert (
                host.capture_cell(cell).has_unread_output is False
                and host.pending_output_cells() == ()
            )
            finish.set()
            async with asyncio.timeout(5):
                while host.snapshot_cell_index():
                    await asyncio.sleep(0.001)
        finally:
            finish.set()
            await host.aclose()
            await port.aclose()

    asyncio.run(run())


def test_process_query_dispatch_reuses_the_live_owner_without_consuming_logs(tmp_path):
    async def run():
        port = runtime(tmp_path)
        try:
            started = await dispatch(
                port,
                "exec_process",
                {
                    "command": "printf 'kept log\\n'; read query_input",
                    "yield_time_ms": 100,
                },
                accept=False,
            )
            target = started.content.public_fields["target"]
            detail = await dispatch(port, "query_processes", {"target": target})
            row = detail.content.content[0].value
            assert (
                row["target"] == target
                and row["command"] == "printf 'kept log\\n'; read query_input"
                and row["process_status"] == "RUNNING"
            )
            inventory = await dispatch(port, "query_processes", {})
            assert [part.value["target"] for part in inventory.content.content] == [
                target
            ]
            pending = port._terminal.wait_process(
                target["process_id"],
                timeout_seconds=0,
                max_output_chars=None,
                since_cursor=port._terminal.model_observation_cursor(
                    target["process_id"], owner_host_session_id=port._terminal_owner_id
                ),
                owner_host_session_id=port._terminal_owner_id,
            )
            assert pending.output == "kept log\n"
        finally:
            await port.aclose()

    asyncio.run(run())


def test_user_cell_index_remains_available_through_host_and_terminal_gateway(tmp_path):
    from pulsara_agent.conversation_kernel.host import KernelHostSession
    from pulsara_agent.terminal_protocol.v3_gateway import (
        TerminalKernelProtocolServer,
        wire,
    )

    index = (
        {
            "cell_id": random_id("cell"),
            "state": "running",
            "has_unread_output": False,
            "pending_calls": 1,
        },
    )
    cell_owner = SimpleNamespace(snapshot_cell_index=lambda: index)
    session = SimpleNamespace(
        _code_mode=cell_owner,
        session_id=random_id("session"),
        host_session_id=random_id("session"),
    )
    session.code_mode_cells = lambda: KernelHostSession.code_mode_cells(session)
    gateway = TerminalKernelProtocolServer(
        socket_path=tmp_path / "unused.sock", session_provider=lambda: None
    )
    frame = gateway._code_mode_cells(
        SimpleNamespace(host_session=session),
        wire.CodeModeCellsRequest(request_id="test"),
    )
    assert frame.code_mode_cells.cells[0].cell_id == index[0]["cell_id"]
    assert frame.code_mode_cells.cells[0].pending_calls == 1
