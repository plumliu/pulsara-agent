"""Exercise tab-close admission, stop settlement, and detach through the local service."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from aiohttp import ClientSession
import pytest

from pulsara_agent.code_mode import CodeModeToolResult
from pulsara_agent.desktop_app.http_server import LocalHttpServer
from pulsara_agent.desktop_app.renderer_bridge import RendererBridge
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.terminal_protocol.v3_gateway import TerminalKernelProtocolServer
from tests.support.model_config import test_model_runtime
from tests.test_agent_session_host import AgentModel
from tests.test_compaction_host_lifecycle import TextModel
from tests.test_desktop_http_surface import _model_server_dependencies_for_runtime
from tests.test_scheduled_tasks_host import setup
from tests.test_stage2_kernel_host_dogfood import _SteerModelPort
from tests.test_stage2_terminal_host_lifetime import _start_background_process

pytestmark = pytest.mark.postgres


@asynccontextmanager
async def tab_service(tmp_path, monkeypatch, database, model):
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    runtime = test_model_runtime(postgres_dsn=database.runtime_dsn)
    core, sessions, service, handle, _ = await setup(tmp_path, database, monkeypatch, model, runtime=runtime)
    with TemporaryDirectory(prefix="pulsara-close-", dir="/tmp") as socket_directory:
        protocol = TerminalKernelProtocolServer(
            socket_path=Path(socket_directory) / "kernel.sock", session_provider=sessions.session_by_host_id,
        )
        await protocol.start()
        bridge = RendererBridge(sessions=sessions, protocol_server=protocol)
        (tmp_path / "index.html").write_text("Session tab close")
        server = LocalHttpServer(
            sessions=sessions, bridge=bridge, static_root=tmp_path, requested_port=0,
            is_ready=lambda: True, is_draining=lambda: False,
            **_model_server_dependencies_for_runtime(runtime),
        )
        await server.start()
        try:
            async with ClientSession(headers={"Origin": server.origin, "Sec-Fetch-Site": "same-origin"}) as client:
                yield core, handle.session, server.origin, client
        finally:
            await server.aclose()
            await bridge.aclose()
            await protocol.close()
            await service.aclose()
            await sessions.aclose()
            await core.shutdown()


async def connect(client, origin, session_id):
    async with client.post(f"{origin}/api/sessions/{session_id}/connections",
                           json={"browser_instance_id": random_id("transport")}) as response:
        payload = await response.json()
        assert response.status == 201, payload
        return payload


def connection_headers(connection):
    return {"X-Pulsara-Connection-Id": connection["connection_id"],
            "X-Pulsara-Connection-Generation": str(connection["connection_generation"])}


async def inspect_close(client, url, controller):
    async with client.get(url, headers=connection_headers(controller)) as response:
        payload = await response.json()
        assert response.status == 200, payload
        return payload


async def stop_observed(client, url, connection, work):
    async with client.post(url, headers=connection_headers(connection),
                           json={"observation_id": work["observation_id"]}) as response:
        return response.status, await response.json()


async def stop_detached_cell(host, client, url, controller):
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def accepted_tool(invocation):
        entered.set()
        await invocation.cancelled.wait()
        cancelled.set()
        await release.wait()
        return CodeModeToolResult(value={"settled": True})

    cell = await host._code_mode.start(
        source="await tools.read_file({});", source_exec_attempt_id=random_id("attempt"),
        turn_id=random_id("turn"), snapshot=None, tool_catalog={"decide": {}, "read_file": {}},
        invoke=accepted_tool, decision_available=False,
    )
    pending = None
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert host.active_root_turn_id() is None
        work = await inspect_close(client, url, controller)
        assert work["requires_confirmation"]
        pending = asyncio.create_task(stop_observed(client, url, controller, work))
        await asyncio.wait_for(cancelled.wait(), 10)
        assert not pending.done()
        release.set()
        assert await pending == (200, {"status": "stopped"})
        assert cell not in host._code_mode.live_cell_ids()
        assert not (await inspect_close(client, url, controller))["requires_confirmation"]
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)


class _SettlingRoot(_SteerModelPort):
    def __init__(self):
        super().__init__()
        self.interrupted = asyncio.Event()
        self.can_settle = asyncio.Event()

    async def _stream(self, request):
        try:
            async for event in super()._stream(request):
                yield event
        except asyncio.CancelledError:
            self.interrupted.set()
            await self.can_settle.wait()
            raise


def test_tab_stop_waits_for_root_without_terminating_background_process(tmp_path, monkeypatch, stage2_migrated_postgres_database):
    async def scenario():
        model = _SettlingRoot()
        async with tab_service(tmp_path, monkeypatch, stage2_migrated_postgres_database, model) as (_, root, origin, client):
            controller = await connect(client, origin, root.session_id)
            observer = await connect(client, origin, root.session_id)
            assert (controller["role"], observer["role"]) == ("controller", "observer")
            url = f"{origin}/api/sessions/{root.session_id}/close-work"
            assert await inspect_close(client, url, controller) == {
                "requires_confirmation": False, "has_terminal_processes": False, "observation_id": None,
            }
            process_id, _, _ = await _start_background_process(root._tools, session_id=root.session_id)
            assert await inspect_close(client, url, controller) == {
                "requires_confirmation": False, "has_terminal_processes": True, "observation_id": None,
            }
            command_id = random_id("command")
            queued_id = random_id("command")
            pending = None
            try:
                await root.submit_prompt(command_id=command_id, content=PromptContent.text("Hold this turn."))
                await asyncio.wait_for(model.started.wait(), 10)
                await root.submit_prompt(command_id=queued_id, content=PromptContent.text("Run after the held turn."))
                work = await inspect_close(client, url, controller)
                assert work["requires_confirmation"] and work["has_terminal_processes"]
                denied, error = await stop_observed(client, url, observer, work)
                assert denied == 409 and error["error"]["code"] == "CHAT_CONTROLLER_REQUIRED"
                async with client.delete(f'{origin}/api/connections/{observer["connection_id"]}') as response:
                    assert response.status in {200, 204}
                assert not model.interrupted.is_set()
                pending = asyncio.create_task(stop_observed(client, url, controller, work))
                await asyncio.wait_for(model.interrupted.wait(), 10)
                assert not pending.done()
                repeat, error = await stop_observed(client, url, controller, work)
                assert repeat == 409 and error["error"]["code"] == "SESSION_CLOSE_WORK_CHANGED"
                model.can_settle.set()
                assert await pending == (200, {"status": "stopped"})
                settled = await root.query_command(command_id)
                assert settled.prompt_delivery.execution.terminal_reason == "USER_STOPPED"
                queued = await root.query_command(queued_id)
                assert queued.prompt_delivery.queue_status == "CANCELLED"
                async with client.delete(f'{origin}/api/connections/{controller["connection_id"]}') as response:
                    assert response.status in {200, 204}
                reopened = await connect(client, origin, root.session_id)
                assert reopened["live_control_snapshot"]["snapshot"]["host_session_id"] == root.host_session_id
                assert [process.process_id for process in root._tools.list_background_terminal_processes()
                        if process.physical_state == "RUNNING"] == [process_id]
            finally:
                model.can_settle.set()
                model.release.set()
                if pending is not None:
                    await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("consumed_during_confirmation", [False, True])
def test_tab_stop_observes_queued_root_and_follows_consumption(
    tmp_path, monkeypatch, stage2_migrated_postgres_database, consumed_during_confirmation,
):
    async def scenario():
        model = _SettlingRoot()
        model.can_settle.set()
        async with tab_service(tmp_path, monkeypatch, stage2_migrated_postgres_database, model) as (_, root, origin, client):
            controller = await connect(client, origin, root.session_id)
            url = f"{origin}/api/sessions/{root.session_id}/close-work"
            preparing, release = asyncio.Event(), asyncio.Event()

            async def pause_delivery(_candidate):
                preparing.set()
                await release.wait()
                return True

            monkeypatch.setattr(root, "_prepare_queued_prompt_hook", pause_delivery)
            command_id = random_id("command")
            try:
                await root.submit_prompt(command_id=command_id, content=PromptContent.text("A pending ROOT input."))
                await asyncio.wait_for(preparing.wait(), 10)
                assert root.active_root_turn_id() is None
                work = await inspect_close(client, url, controller)
                assert work["requires_confirmation"]
                if consumed_during_confirmation:
                    release.set()
                    await asyncio.wait_for(model.started.wait(), 10)
                assert await stop_observed(client, url, controller, work) == (200, {"status": "stopped"})
                settled = await root.query_command(command_id)
                if consumed_during_confirmation:
                    assert settled.prompt_delivery.execution.terminal_reason == "USER_STOPPED"
                else:
                    assert settled.prompt_delivery.queue_status == "CANCELLED"
                    assert not model.started.is_set()
            finally:
                release.set()
                model.release.set()

    asyncio.run(scenario())


class _BlockedAgents(AgentModel):
    async def stream(self, request):
        if request.session_id != self.root_id and self.child_requests:
            await self.first_child_release.wait()
        async for event in super().stream(request):
            yield event


def test_tab_stop_cancels_observed_agent_requests_after_root_reply(tmp_path, monkeypatch, stage2_migrated_postgres_database):
    async def scenario():
        model = _BlockedAgents()
        async with tab_service(tmp_path, monkeypatch, stage2_migrated_postgres_database, model) as (core, root, origin, client):
            model.root_id = root.session_id
            controller = await connect(client, origin, root.session_id)
            url = f"{origin}/api/sessions/{root.session_id}/close-work"

            async def delegate():
                await asyncio.wait_for(root.run_turn(
                    PromptContent.text("Delegate a separate assignment."), command_id=random_id("command"),
                    requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                ), 15)
                assert root.active_root_turn_id() is None

            async def works():
                return await root._io.run(root.repository.list_agent_works, root_session_id=root.session_id,
                    agent_id=model.agent_id, deadline_monotonic=root._canonical_deadline())

            try:
                await delegate()
                await asyncio.wait_for(model.first_child_started.wait(), 15)
                (model.agent_id,) = await root._io.run(root.repository.list_root_agent_session_ids,
                    root_session_id=root.session_id, deadline_monotonic=root._canonical_deadline())
                await delegate()
                work = await inspect_close(client, url, controller)
                assert work["requires_confirmation"] and not work["has_terminal_processes"]
                await delegate()
                assert [item["status"] for item in await works()] == ["running", "queued", "queued"]
                status, error = await stop_observed(client, url, controller, work)
                assert status == 409 and error["error"]["code"] == "SESSION_CLOSE_WORK_PENDING"
                first, second, third = await works()
                assert [first["status"], second["status"]] == ["stopped", "stopped"]
                assert third["status"] in {"queued", "running"}
                work = await inspect_close(client, url, controller)
                assert await stop_observed(client, url, controller, work) == (200, {"status": "stopped"})
                assert [item["status"] for item in await works()] == ["stopped"] * 3
                assert not (await inspect_close(client, url, controller))["requires_confirmation"]
                await stop_detached_cell(core._live_agent_session(model.agent_id), client, url, controller)
            finally:
                model.first_child_release.set()

    asyncio.run(scenario())


def test_tab_stop_joins_idle_compaction_and_detached_cell_tools(tmp_path, monkeypatch, stage2_migrated_postgres_database):
    async def scenario():
        async with tab_service(tmp_path, monkeypatch, stage2_migrated_postgres_database, TextModel()) as (_, root, origin, client):
            controller = await connect(client, origin, root.session_id)
            url = f"{origin}/api/sessions/{root.session_id}/close-work"
            await root.run_turn(PromptContent.text("Make a completed turn for manual compaction."),
                                command_id=random_id("command"))
            entered, release = asyncio.Event(), asyncio.Event()

            async def blocking_summary(**kwargs):
                entered.set()
                await release.wait()
                raise RuntimeError("The test never releases an active compaction normally")

            monkeypatch.setattr(root._runner, "compact_idle_turn", blocking_summary)
            compaction = asyncio.create_task(root.compact_context(command_id=random_id("command")))
            try:
                await asyncio.wait_for(entered.wait(), 10)
                assert root.active_root_turn_id() is None
                work = await inspect_close(client, url, controller)
                assert work["requires_confirmation"]
                assert await stop_observed(client, url, controller, work) == (200, {"status": "stopped"})
                assert (await compaction).public_code == "COMPACTION_CANCELLED"
            finally:
                release.set()
                await asyncio.gather(compaction, return_exceptions=True)

            await stop_detached_cell(root, client, url, controller)

    asyncio.run(scenario())
