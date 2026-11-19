"""Close and reopen real session tabs through HTTP, the protocol gateway, and PostgreSQL."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from aiohttp import ClientSession
import pytest

from pulsara_agent.capability.management_form import CapabilityFormValues
from pulsara_agent.desktop_app.http_server import LocalHttpServer
from pulsara_agent.desktop_app.protocol_client import TerminalProtocolClient
from pulsara_agent.desktop_app.renderer_bridge import RendererBridge
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.run_permission import RunPermissionAdmissionSource, build_run_permission_snapshot
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from pulsara_agent.terminal_protocol.v3_gateway import TerminalKernelProtocolServer
from tests.support.model_config import test_model_runtime
from tests.test_blocking_steer_host import _wait_for_card
from tests.test_capability_form_interaction import open_form
from tests.test_compaction_host_lifecycle import TextModel
from tests.test_desktop_http_surface import _model_server_dependencies_for_runtime
from tests.test_scheduled_tasks_host import setup
from tests.test_stage2_kernel_host_dogfood import _SteerModelPort
from tests.test_tool_permission_settlement import ApprovalModel, _accepted_result, _assert_delivery, UNAUTHORIZED_TEXT

pytestmark = pytest.mark.postgres


@pytest.fixture
def form_permission():
    return build_run_permission_snapshot(
        snapshot_id=random_id("permission"), requested_mode=PermissionMode.READ_ONLY,
        effective_mode=PermissionMode.READ_ONLY, admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
    )


@asynccontextmanager
async def tab_service(tmp_path, monkeypatch, database, model):
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    runtime = test_model_runtime(postgres_dsn=database.runtime_dsn)
    core, sessions, service, handle, _ = await setup(tmp_path, database, monkeypatch, model, runtime=runtime)
    with TemporaryDirectory(prefix="pulsara-close-", dir="/tmp") as socket_directory:
        protocol = TerminalKernelProtocolServer(
            socket_path=Path(socket_directory) / "kernel.sock", session_provider=sessions.session_by_host_id,
            on_attachment_closed=sessions.attachment_closed, wait_for_idle_unload=core.wait_for_idle_unload,
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
                yield sessions, handle.session, server.origin, client, protocol
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


async def close_tab(client, origin, session_id, connection, confirmation=None):
    confirmation = confirmation or {}
    async with client.post(
        f"{origin}/api/sessions/{session_id}/close-tab", headers=connection_headers(connection),
        json={"confirmed_form_ids": confirmation.get("form_ids", []),
              "confirmed_other_clients": confirmation.get("other_clients", False)},
    ) as response:
        result = await response.json()
        assert response.status == 200, result
        return result


async def wait_until(check):
    async with asyncio.timeout(8):
        while not check():
            await asyncio.sleep(0.015)


def test_direct_and_browser_observers_are_counted_and_last_disconnect_releases_host(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    async def scenario():
        async with tab_service(tmp_path, monkeypatch, stage2_migrated_postgres_database, TextModel()) as (
            sessions, host, origin, client, protocol,
        ):
            owner = await connect(client, origin, host.session_id)
            viewer = await connect(client, origin, host.session_id)
            direct, _ = await TerminalProtocolClient.connect(
                server=protocol, host_session_id=host.host_session_id, session_id=host.session_id,
                role=wire.ATTACHMENT_ROLE_OBSERVER, client_instance_id=random_id("transport"),
            )
            try:
                for connection in (viewer, owner):
                    prompt = await close_tab(client, origin, host.session_id, connection)
                    assert prompt == {"status": "confirmation_required", "form_ids": [],
                                      "visible_form": False, "other_clients": True}
                    assert await close_tab(client, origin, host.session_id, connection, prompt) == {"status": "closed"}
                assert host.host_session_id in sessions.core._sessions
            finally:
                # A direct peer can close its Unix socket without a browser notification.
                direct._writer.close()
                await direct._writer.wait_closed()
            await wait_until(lambda: host.host_session_id not in sessions.core._sessions)
            reopened = await connect(client, origin, host.session_id)
            assert reopened["live_control_snapshot"]["snapshot"]["host_session_id"] != host.host_session_id
            assert reopened["role"] == "controller"
            assert (await sessions.read_session(host.session_id))["lifecycle"] == "OPEN"

    asyncio.run(scenario())


def test_background_turn_reopens_same_host_and_late_transport_cleanup_cannot_unload_it(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    async def scenario():
        model = _SteerModelPort()
        async with tab_service(tmp_path, monkeypatch, stage2_migrated_postgres_database, model) as (
            sessions, host, origin, client, _,
        ):
            first = await connect(client, origin, host.session_id)
            running = asyncio.create_task(host.run_turn(PromptContent.text("Keep working.")))
            try:
                await asyncio.wait_for(model.started.wait(), 5)
                assert await close_tab(client, origin, host.session_id, first) == {"status": "closed"}
                assert not running.done()
                assert host.session_id in sessions._pending_tab_unloads
                reopened = await connect(client, origin, host.session_id)
                assert reopened["live_control_snapshot"]["snapshot"]["host_session_id"] == host.host_session_id
                assert host.session_id not in sessions._pending_tab_unloads
                for connection in (first, reopened):
                    async with client.delete(f'{origin}/api/connections/{connection["connection_id"]}') as reply:
                        assert reply.status == 200
            finally:
                model.release.set()
                await asyncio.wait_for(running, 5)
            assert not sessions._pending_tab_unloads
            assert not host._closing

    asyncio.run(scenario())


def test_tab_confirmation_rechecks_new_forms_and_only_revokes_its_controller(
    tmp_path, monkeypatch, stage2_migrated_postgres_database, form_permission,
):
    async def scenario():
        async with tab_service(tmp_path, monkeypatch, stage2_migrated_postgres_database, TextModel()) as (
            _, host, origin, client, _protocol,
        ):
            owner = await connect(client, origin, host.session_id)
            viewer = await connect(client, origin, host.session_id)

            async def must_not_submit(_):
                pytest.fail("closing a tab cannot submit form input")

            first = await open_form(host._interactions, form_permission, must_not_submit)
            prompt = await close_tab(client, origin, host.session_id, owner)
            assert prompt["visible_form"] and prompt["other_clients"] and len(prompt["form_ids"]) == 1
            second = await open_form(host._interactions, form_permission, must_not_submit)
            updated = await close_tab(client, origin, host.session_id, owner, prompt)
            assert updated["status"] == "confirmation_required" and len(updated["form_ids"]) == 2
            viewer_prompt = await close_tab(client, origin, host.session_id, viewer)
            assert viewer_prompt["form_ids"] == [] and viewer_prompt["other_clients"]
            assert await close_tab(client, origin, host.session_id, viewer, viewer_prompt) == {"status": "closed"}
            assert not first.done() and not second.done()
            assert await close_tab(client, origin, host.session_id, owner, updated) == {"status": "closed"}
            for form in (first, second):
                assert (await form).reference == "interaction:controller-detached"

    asyncio.run(scenario())


def test_closed_tab_expiry_waits_for_workspace_and_final_model_delivery_before_unload(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    from pulsara_agent.conversation_kernel import interaction

    async def scenario():
        model = ApprovalModel("write_file", {"target": {"kind": "PATH", "path": "blocked.txt"}, "content": "blocked"})
        monkeypatch.setattr(interaction, "INTERACTION_TIMEOUT_SECONDS", 0.6)
        async with tab_service(tmp_path, monkeypatch, stage2_migrated_postgres_database, model) as (
            sessions, host, origin, client, _,
        ):
            owner = await connect(client, origin, host.session_id)
            workspace = host.workspace.workspace_root
            moved = workspace.with_name("moved")
            running = asyncio.create_task(host.run_turn(PromptContent.text("Write a file."),
                                                       requested_permission_mode=PermissionMode.ASK_PERMISSIONS))
            try:
                await _wait_for_card(host)
                assert await close_tab(client, origin, host.session_id, owner) == {"status": "closed"}
                workspace.rename(moved)
                row = await _accepted_result(host)
                assert row["result_state"] == "CANCELLED_BEFORE_DISPATCH" and not row["attempts"]
                assert not running.done() and not host._closing
                assert host.live_control.current_snapshot().current_interaction is None
            finally:
                if moved.exists():
                    moved.rename(workspace)
                host.workspace_gate.wake()
                await asyncio.wait_for(running, 8)
            _assert_delivery(host, model, row, "CANCELLED", UNAUTHORIZED_TEXT)
            assert not (workspace / "blocked.txt").exists()
            await wait_until(lambda: host.host_session_id not in sessions.core._sessions)

    asyncio.run(scenario())


@pytest.mark.parametrize("close_confirmed", [True, False], ids=["closed", "unconfirmed"])
def test_open_during_committed_unload_waits_then_returns_live_or_saved_history(
    tmp_path, monkeypatch, stage2_migrated_postgres_database, close_confirmed,
):
    async def scenario():
        async with tab_service(tmp_path, monkeypatch, stage2_migrated_postgres_database, TextModel()) as (
            sessions, host, origin, client, _protocol,
        ):
            owner = await connect(client, origin, host.session_id)
            await host.run_turn(PromptContent.text("Keep this in history."))
            closing = asyncio.Event()
            release = asyncio.Event()
            original_close = sessions.core._close_session_owner

            async def settle(**kwargs):
                closing.set()
                await release.wait()
                await original_close(**kwargs)
                if not close_confirmed:
                    raise RuntimeError("could not confirm settlement")

            monkeypatch.setattr(sessions.core, "_close_session_owner", settle)
            assert await close_tab(client, origin, host.session_id, owner) == {"status": "closed"}
            await asyncio.wait_for(closing.wait(), 5)
            opening = asyncio.create_task(connect(client, origin, host.session_id))
            try:
                await asyncio.sleep(0.05)
                assert not opening.done()
            finally:
                release.set()
            view = await asyncio.wait_for(opening, 8)
            if close_confirmed:
                assert view["role"] == "controller"
                assert view["live_control_snapshot"]["snapshot"]["host_session_id"] != host.host_session_id
            else:
                assert view["view"] == "history" and view["runtime_close_failed"]
                assert "connection_id" not in view
                assert view["snapshot"]["snapshot"]["snapshot"]["entries"]
                again = await connect(client, origin, host.session_id)
                assert again["view"] == "history" and again["runtime_close_failed"]

    asyncio.run(scenario())


def test_transport_reconnect_preserves_existing_intent_and_only_explicit_open_clears_it(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    async def scenario():
        model = _SteerModelPort()
        async with tab_service(tmp_path, monkeypatch, stage2_migrated_postgres_database, model) as (
            sessions, host, origin, client, _,
        ):
            owner = await connect(client, origin, host.session_id)
            running = asyncio.create_task(host.run_turn(PromptContent.text("Keep running.")))
            try:
                await asyncio.wait_for(model.started.wait(), 5)
                assert await close_tab(client, origin, host.session_id, owner) == {"status": "closed"}
                async with client.post(f"{origin}/api/sessions/{host.session_id}/connections", json={
                    "browser_instance_id": random_id("transport"), "reopen_tab": False,
                }) as res:
                    assert res.status == 201
                    recovered = await res.json()
                assert recovered["live_control_snapshot"]["snapshot"]["host_session_id"] == host.host_session_id
                assert host.session_id in sessions._pending_tab_unloads
            finally:
                model.release.set()
                await asyncio.wait_for(running, 5)
            assert not host._closing
            async with client.delete(f'{origin}/api/connections/{recovered["connection_id"]}') as res:
                assert res.status == 200
            await wait_until(lambda: host.host_session_id not in sessions.core._sessions)

    asyncio.run(scenario())


def test_direct_protocol_hello_during_idle_check_keeps_exact_host(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    async def scenario():
        async with tab_service(tmp_path, monkeypatch, stage2_migrated_postgres_database, TextModel()) as (
            sessions, host, origin, client, protocol,
        ):
            owner = await connect(client, origin, host.session_id)
            checking, release = asyncio.Event(), asyncio.Event()
            run = host._io.run

            async def pause_check(operation, *args, **kwargs):
                if operation == host.repository.archive_session and kwargs.get("check_only"):
                    checking.set()
                    await release.wait()
                return await run(operation, *args, **kwargs)

            monkeypatch.setattr(host._io, "run", pause_check)
            assert await close_tab(client, origin, host.session_id, owner) == {"status": "closed"}
            await asyncio.wait_for(checking.wait(), 5)
            joining = asyncio.create_task(TerminalProtocolClient.connect(
                server=protocol, host_session_id=host.host_session_id, session_id=host.session_id,
                role=wire.ATTACHMENT_ROLE_CONTROLLER, client_instance_id=random_id("transport"),
            ))
            try:
                await wait_until(lambda: protocol.has_session_attachments(host, include_pending=True))
                assert not joining.done()
            finally:
                release.set()
            direct, hello = await asyncio.wait_for(joining, 5)
            try:
                assert host.has_controller_attachment(hello.attachment_id)
                assert not host._closing
            finally:
                await direct.aclose()
            await wait_until(lambda: host.host_session_id not in sessions.core._sessions)

    asyncio.run(scenario())


@pytest.mark.parametrize("submitted", [True, False], ids=["transferred", "validating"])
def test_close_warns_only_until_form_input_transfers(
    tmp_path, monkeypatch, stage2_migrated_postgres_database, form_permission, submitted,
):
    async def scenario():
        async with tab_service(tmp_path, monkeypatch, stage2_migrated_postgres_database, TextModel()) as (
            _, host, origin, client, _protocol,
        ):
            owner = await connect(client, origin, host.session_id)
            entered, release = asyncio.Event(), asyncio.Event()

            async def validate(body):
                entered.set()
                await release.wait()
                return CapabilityFormValues(freeze_json(body))

            form = await open_form(host._interactions, form_permission, validate)
            snapshot = host.live_control.current_snapshot()
            saving = asyncio.create_task(host.resolve_capability_form(
                attachment_id=host._interactions.current_controller_id(),
                interaction_id=snapshot.current_interaction.interaction_id,
                expected_owner_epoch=snapshot.owner_epoch, expected_live_revision=snapshot.revision,
                decision="SUBMIT", submission={"address": "B"},
            ))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                if submitted:
                    release.set()
                    await saving
                result = await close_tab(client, origin, host.session_id, owner)
                if not submitted:
                    assert result["visible_form"] and result["form_ids"] == [snapshot.current_interaction.interaction_id]
                    result = await close_tab(client, origin, host.session_id, owner, result)
                assert result == {"status": "closed"}
            finally:
                release.set()
                await saving
            assert (await form).decision == ("SUBMIT" if submitted else "CANCEL")

    asyncio.run(scenario())
