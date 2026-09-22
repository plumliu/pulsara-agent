"""Deletion joins real owners, survives waiter loss and confirms ambiguous ACKs."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pulsara_agent.conversation_kernel.session_deletion import SessionDeleteRejected
from pulsara_agent.web_app.browser_bridge import LocalBrowserBridge
from tests.test_local_web_browser_bridge import (
    _runtime_reopen_controller,
    _RuntimeReopenHost,
    _RuntimeReopenConnection,
)


def control(tmp_path, *, close_fails=False, connection_fails=False):
    old = _RuntimeReopenHost("session:delete", "host:delete")
    controller, core = _runtime_reopen_controller(tmp_path, old_host=old)
    controller._fork_sessions = {}
    operation = SimpleNamespace(physical_full=False)
    core.canonical_session_exists = AsyncMock(return_value=True)
    core.begin_session_deletion = AsyncMock(return_value=operation)

    async def stop(_):
        if close_fails:
            raise RuntimeError("physical owner still running")
        operation.physical_full = True

    core.quiesce_session_deletion = AsyncMock(side_effect=stop)
    core.commit_session_deletion = AsyncMock(return_value="DELETED")
    core.finish_session_deletion = AsyncMock()
    bridge = LocalBrowserBridge(sessions=controller, protocol_server=object())
    connection = _RuntimeReopenConnection(
        "connection:delete", old.session_id, fail=connection_fails
    )
    bridge._connections[connection.connection_id] = connection
    bridge._controller_by_session[old.session_id] = connection.connection_id
    return controller, core, bridge, old, connection


@pytest.mark.parametrize("phase", ["bridge", "host"])
def test_physical_close_failure_keeps_data_and_quarantines(tmp_path, phase):
    async def run():
        c, core, b, s, connection = control(
            tmp_path, close_fails=phase == "host", connection_fails=phase == "bridge"
        )
        with pytest.raises(SessionDeleteRejected) as error:
            await c.delete_session(s.session_id, bridge=b)
        assert error.value.public_code == "SESSION_DELETE_QUARANTINED"
        core.commit_session_deletion.assert_not_awaited()
        assert connection.close_attempted
        assert s.session_id in b._quarantined_sessions
        with pytest.raises(SessionDeleteRejected):
            await c.delete_session(s.session_id, bridge=b)

    asyncio.run(run())


def test_disconnected_waiter_and_duplicate_delete_join_same_worker(tmp_path):
    async def run():
        c, core, b, s, connection = control(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()

        async def commit(_):
            entered.set()
            await release.wait()
            return "DELETED"

        core.commit_session_deletion.side_effect = commit
        first = asyncio.create_task(c.delete_session(s.session_id, bridge=b))
        await entered.wait()
        assert b._session_locks[s.session_id].locked()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        duplicate = asyncio.create_task(c.delete_session(s.session_id, bridge=b))
        await asyncio.sleep(0)
        assert not duplicate.done()
        core.commit_session_deletion.assert_awaited_once()
        release.set()
        assert await duplicate == {"status": "DELETED", "session_id": s.session_id}
        assert connection.close_attempted
        assert s.session_id not in c._by_session
        assert s.session_id not in c._operations
        assert s.session_id not in b._session_locks

    asyncio.run(run())


@pytest.mark.parametrize("exists_after", [True, False])
def test_ambiguous_commit_uses_canonical_existence_not_resumability(
    tmp_path, exists_after
):
    async def run():
        c, core, b, s, _ = control(tmp_path)
        core.commit_session_deletion.side_effect = OSError("ACK lost")
        core.canonical_session_exists.side_effect = [True, exists_after]
        if exists_after:
            with pytest.raises(SessionDeleteRejected) as error:
                await c.delete_session(s.session_id, bridge=b)
            assert error.value.public_code == "SESSION_DELETE_FAILED"
        else:
            assert (await c.delete_session(s.session_id, bridge=b))[
                "status"
            ] == "ABSENT"
        assert s.session_id not in c._operations
        assert not b._session_locks

    asyncio.run(run())


def test_unreadable_database_retry_confirms_only_and_retains_gate(tmp_path):
    async def run():
        c, core, b, s, _ = control(tmp_path)
        core.commit_session_deletion.side_effect = OSError("ACK lost")
        core.canonical_session_exists.side_effect = [True, OSError("offline"), False]
        with pytest.raises(SessionDeleteRejected) as error:
            await c.delete_session(s.session_id, bridge=b)
        assert error.value.public_code == "SESSION_DELETE_UNCONFIRMED"
        assert b._session_locks[s.session_id].locked()
        assert (await c.delete_session(s.session_id, bridge=b))["status"] == "ABSENT"
        core.commit_session_deletion.assert_awaited_once()
        core.quiesce_session_deletion.assert_awaited_once()
        assert not b._session_locks

    asyncio.run(run())


def test_postcommit_cleanup_failure_does_not_report_rollback(tmp_path):
    async def run():
        c, core, b, s, _ = control(tmp_path)
        b.settle_session_delete_detach = AsyncMock(
            side_effect=RuntimeError("cleanup failed")
        )
        assert (await c.delete_session(s.session_id, bridge=b))["status"] == "DELETED"
        assert s.session_id in c._operations
        core.finish_session_deletion.assert_awaited_once()
        assert core.finish_session_deletion.await_args.kwargs["quarantine"]

    asyncio.run(run())


def test_deletion_http_is_strict_and_old_close_has_separate_route(tmp_path):
    from aiohttp import ClientSession
    from pulsara_agent.web_app.http_server import LocalHttpServer
    from pulsara_agent.web_app.application import packaged_static_root
    from tests.test_local_web_http_surface import _model_server_dependencies, _Bridge

    async def run():
        sessions = SimpleNamespace(
            delete_session=AsyncMock(
                return_value={"status": "DELETED", "session_id": "target"}
            ),
            prepare_raw_close=AsyncMock(return_value=None),
        )
        bridge = _Bridge()
        server = LocalHttpServer(
            sessions=sessions,
            bridge=bridge,
            static_root=packaged_static_root(),
            requested_port=0,
            is_ready=lambda: True,
            is_draining=lambda: False,
            **_model_server_dependencies(),
        )
        await server.start()
        try:
            async with ClientSession() as client:
                url = f"{server.origin}/api/sessions/target"
                for body in (
                    {},
                    {"confirm_permanent_delete": False},
                    {"confirm_permanent_delete": 1},
                    {"close_conversation": True},
                    {"confirm_permanent_delete": True, "workspace": "extra"},
                ):
                    async with client.delete(url, json=body) as response:
                        assert response.status == 400
                sessions.delete_session.assert_not_awaited()
                async with client.delete(
                    url,
                    json={"confirm_permanent_delete": True},
                    headers={"Origin": "https://elsewhere.example"},
                ) as response:
                    assert response.status == 403
                async with client.delete(
                    url, json={"confirm_permanent_delete": True}
                ) as response:
                    assert response.status == 200
                    assert (await response.json())["status"] == "DELETED"
                sessions.delete_session.assert_awaited_once_with(
                    "target", bridge=bridge
                )
                async with client.post(
                    url + "/close", json={"close_conversation": False}
                ) as response:
                    assert response.status == 200
                sessions.prepare_raw_close.assert_awaited_once_with(
                    "target", close_conversation=False
                )
        finally:
            await server.aclose()

    asyncio.run(run())


def test_core_deletion_waits_admitted_open_and_rejects_new_admission():
    from pulsara_agent.conversation_kernel.host import KernelHostCore

    async def run():
        core = object.__new__(KernelHostCore)
        core._lock = asyncio.Lock()
        core._closing = False
        core._open_attempts = {}
        core._session_deletions = {}
        admitted = await core._admit_session_open("parent", "child")
        pending = asyncio.create_task(core.begin_session_deletion("child", "u_local"))
        await asyncio.sleep(0)
        assert not pending.done()
        with pytest.raises(SessionDeleteRejected):
            await core._admit_session_open("child")
        await core._settle_session_open(admitted)
        operation = await pending
        await core.finish_session_deletion(operation, quarantine=False)
        assert not core._open_attempts and not core._session_deletions

    asyncio.run(run())


def test_core_commit_joins_physical_database_worker_even_after_waiter_cancel():
    from threading import Event
    from time import monotonic
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.conversation_kernel.session_deletion import KernelSessionDeletion

    async def run():
        core = object.__new__(KernelHostCore)
        entered, release = Event(), Event()

        def delete(**_):
            entered.set()
            assert release.wait(10)
            return "DELETED"

        core._ensure_resources = AsyncMock(
            return_value=SimpleNamespace(delete_session=delete)
        )
        core._canonical_deadline = lambda: monotonic() + 0.05
        op = KernelSessionDeletion(
            "target",
            "u_local",
            core,
            asyncio.get_running_loop().create_future(),
            physical_full=True,
        )
        core._session_deletions = {"target": op}
        pending = asyncio.create_task(core.commit_session_deletion(op))
        assert await asyncio.to_thread(entered.wait, 10)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        release.set()
        assert await pending == "DELETED"

    asyncio.run(run())
