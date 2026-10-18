"""Deletion joins real owners, survives waiter loss and confirms ambiguous ACKs."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pulsara_agent.conversation_kernel.session_deletion import SessionDeleteRejected
from pulsara_agent.desktop_app.renderer_bridge import RendererBridge
from tests.test_desktop_renderer_bridge import (
    _runtime_reopen_controller,
    _RuntimeReopenHost,
    _RuntimeReopenConnection,
)


def control(tmp_path, *, close_fails=False, connection_fails=False):
    old = _RuntimeReopenHost('session:xebqi4u7daptmer2vee5wfjfxe', 'host:ut26hrctidbjczdrnrocppmdka')
    controller, core = _runtime_reopen_controller(tmp_path, old_host=old)
    controller._fork_sessions = {}
    operation = SimpleNamespace(physical_full=False)
    core.canonical_session_exists = AsyncMock(return_value=True)
    core.read_session_lifecycle = AsyncMock(return_value='ARCHIVED')
    core.begin_session_retirement = AsyncMock(return_value=operation)

    async def stop(_):
        if close_fails:
            raise RuntimeError("physical owner still running")
        operation.physical_full = True

    core.quiesce_session_retirement = AsyncMock(side_effect=stop)
    core.commit_session_deletion = AsyncMock(return_value="DELETED")
    core.finish_session_retirement = AsyncMock()
    bridge = RendererBridge(sessions=controller, protocol_server=object())
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


@pytest.mark.parametrize('action', ['delete', 'delete_archived'])
def test_disconnected_waiter_and_duplicate_delete_join_same_worker(tmp_path, action):
    async def run():
        c, core, b, s, connection = control(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()

        async def commit(_, **kwargs):
            entered.set()
            await release.wait()
            return "DELETED"

        core.commit_session_deletion.side_effect = commit
        first = asyncio.create_task(c.delete_session(s.session_id, bridge=b, action=action))
        await entered.wait()
        assert b._session_locks[s.session_id].locked()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        duplicate = asyncio.create_task(c.delete_session(s.session_id, bridge=b, action=action))
        await asyncio.sleep(0)
        assert not duplicate.done()
        core.commit_session_deletion.assert_awaited_once()
        release.set()
        expected = {"status": "DELETED", "session_id": s.session_id}
        if action == 'delete_archived':
            expected['cleanup_warning'] = None
        assert await duplicate == expected
        assert core.commit_session_deletion.await_args.kwargs == ({'required_lifecycle': 'ARCHIVED'} if action == 'delete_archived' else {})
        assert connection.close_attempted
        assert s.session_id not in c._by_session
        assert s.session_id not in c._operations
        assert s.session_id not in b._session_locks

    asyncio.run(run())


@pytest.mark.parametrize('lifecycle,status', [('OPEN', 'NOT_ARCHIVED'), (None, 'ABSENT')])
def test_archived_delete_preflight_leaves_runtime_and_connections_untouched(tmp_path, lifecycle, status):
    async def run():
        c, core, bridge, session, connection = control(tmp_path)
        core.read_session_lifecycle.return_value = lifecycle
        assert await c.delete_session(session.session_id, bridge=bridge, action='delete_archived') == {
            'status': status, 'session_id': session.session_id, 'cleanup_warning': None,
        }
        core.commit_session_deletion.assert_not_awaited()
        core.quiesce_session_retirement.assert_not_awaited()
        assert not connection.close_attempted
        assert session.session_id in c._by_session
        assert session.session_id not in c._operations
        core.finish_session_retirement.assert_awaited_once_with(core.begin_session_retirement.return_value, quarantine=False)
    asyncio.run(run())


@pytest.mark.parametrize('release_fails', [False, True])
def test_archived_delete_preflight_read_failure_is_not_ambiguous_commit(tmp_path, release_fails):
    async def run():
        c, core, bridge, session, connection = control(tmp_path)
        core.read_session_lifecycle.side_effect = OSError('temporary database failure')
        if release_fails:
            core.finish_session_retirement.side_effect = RuntimeError('release failed')
        with pytest.raises(SessionDeleteRejected) as error:
            await c.delete_session(session.session_id, bridge=bridge, action='delete_archived')
        assert error.value.public_code == ('SESSION_DELETE_QUARANTINED' if release_fails else 'SESSION_DELETE_PREFLIGHT_FAILED')
        assert not connection.close_attempted
        core.commit_session_deletion.assert_not_awaited()
        core.quiesce_session_retirement.assert_not_awaited()
        assert session.session_id in c._by_session
        if not release_fails:
            assert session.session_id not in c._operations
            core.read_session_lifecycle.side_effect = None
            assert (await c.delete_session(session.session_id, bridge=bridge, action='delete_archived'))['status'] == 'DELETED'
        else:
            assert not hasattr(c._operations[session.session_id], 'unconfirmed')
    asyncio.run(run())


@pytest.mark.parametrize('status,preflight', [('NOT_ARCHIVED', True), ('NOT_ARCHIVED', False), ('DELETED', False), ('ABSENT', False)])
def test_archived_delete_keeps_data_result_and_exposes_cleanup_quarantine(tmp_path, status, preflight):
    async def run():
        c, core, bridge, session, _ = control(tmp_path)
        if preflight:
            core.read_session_lifecycle.return_value = 'OPEN'
        core.commit_session_deletion.return_value = status
        core.finish_session_retirement.side_effect = RuntimeError('cleanup failed')
        result = await c.delete_session(session.session_id, bridge=bridge, action='delete_archived')
        assert result['status'] == status
        assert result['cleanup_warning']['code'] == 'SESSION_DELETE_QUARANTINED'
        assert '重启 Pulsara' in result['cleanup_warning']['message']
        assert session.session_id in c._operations
    asyncio.run(run())


def test_archived_delete_unconfirmed_retry_only_confirms_original_worker(tmp_path):
    async def run():
        c, core, bridge, session, _ = control(tmp_path)
        core.commit_session_deletion.side_effect = OSError('ACK lost')
        core.canonical_session_exists.side_effect = [True, OSError('offline'), False]
        with pytest.raises(SessionDeleteRejected, match='暂时无法确认'):
            await c.delete_session(session.session_id, bridge=bridge, action='delete_archived')
        from pulsara_agent.desktop_app.session_controller import SessionControlRejected
        with pytest.raises(SessionControlRejected):
            await c.delete_session(session.session_id, bridge=bridge)
        result = await c.delete_session(session.session_id, bridge=bridge, action='delete_archived')
        assert result == {'status': 'ABSENT', 'session_id': session.session_id, 'cleanup_warning': None}
        core.commit_session_deletion.assert_awaited_once()
        core.read_session_lifecycle.assert_awaited_once()
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
        core.quiesce_session_retirement.assert_awaited_once()
        assert not b._session_locks

    asyncio.run(run())


def test_postcommit_cleanup_failure_does_not_report_rollback(tmp_path):
    async def run():
        c, core, b, s, _ = control(tmp_path)
        b.settle_session_retirement_detach = AsyncMock(
            side_effect=RuntimeError("cleanup failed")
        )
        assert (await c.delete_session(s.session_id, bridge=b))["status"] == "DELETED"
        assert s.session_id in c._operations
        core.finish_session_retirement.assert_awaited_once()
        assert core.finish_session_retirement.await_args.kwargs["quarantine"]

    asyncio.run(run())


def test_deletion_http_is_strict_and_old_close_has_separate_route(tmp_path):
    from aiohttp import ClientSession
    from pulsara_agent.desktop_app.http_server import LocalHttpServer
    from pulsara_agent.desktop_app.application import packaged_static_root
    from tests.test_desktop_http_surface import _model_server_dependencies, _Bridge

    async def run():
        sessions = SimpleNamespace(
            delete_session=AsyncMock(
                return_value={"status": "DELETED", "session_id": 'session:zj56xaiwutqcu36hnvrihidlzq'}
            ),
            prepare_raw_close=AsyncMock(return_value=None),
            archive_session=AsyncMock(return_value={"status": "ARCHIVED", "session_id": 'session:zj56xaiwutqcu36hnvrihidlzq'}),
            unarchive_session=AsyncMock(return_value={"status": "OPEN", "session_id": 'session:zj56xaiwutqcu36hnvrihidlzq'}),
            list_archived_sessions=AsyncMock(return_value=[{"session_id": 'session:zj56xaiwutqcu36hnvrihidlzq', "lifecycle": "ARCHIVED"}]),
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
            async with ClientSession(headers={"X-Pulsara-Connection-Id":"connection-1", "X-Pulsara-Connection-Generation":"1"}) as client:
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
                    url + "/close", json={}
                ) as response:
                    assert response.status == 200
                sessions.prepare_raw_close.assert_awaited_once_with(
                    "target"
                )
                async with client.post(url + '/close', json={'close_conversation': True}) as response:
                    assert response.status == 400
                for action, status in (('archive', 'ARCHIVED'), ('unarchive', 'OPEN')):
                    async with client.post(url + '/' + action, json={'force': True}) as response:
                        assert response.status == 400
                    async with client.post(url + '/' + action, json={}, headers={'Origin': 'https://elsewhere.example'}) as response:
                        assert response.status == 403
                    async with client.post(url + '/' + action, json={}) as response:
                        assert response.status == 200
                        assert (await response.json())['status'] == status
                sessions.archive_session.assert_awaited_once_with('target', bridge=bridge)
                sessions.unarchive_session.assert_awaited_once_with('target')
                for action, code, status in (
                    ('archive', 'SESSION_ARCHIVE_BUSY', 409),
                    ('archive', 'SESSION_NOT_IDLE', 409),
                    ('archive', 'SESSION_ARCHIVE_UNCONFIRMED', 503),
                    ('unarchive', 'SESSION_ARCHIVE_FAILED', 500),
                    ('delete', 'SESSION_DELETE_BUSY', 409),
                    ('delete', 'SESSION_DELETE_UNCONFIRMED', 503),
                ):
                    owner = sessions.delete_session if action == 'delete' else getattr(sessions, action + '_session')
                    owner.side_effect = SessionDeleteRejected(code, '会话控制请求未完成', status)
                    method = client.delete if action == 'delete' else client.post
                    endpoint = url if action == 'delete' else url + '/' + action
                    body = {'confirm_permanent_delete': True} if action == 'delete' else {}
                    async with method(endpoint, json=body) as response:
                        assert response.status == status
                        assert response.content_type == 'application/json'
                        assert await response.json() == {'error': {
                            'code': code, 'message': '会话控制请求未完成', 'retryable': True,
                        }}
                    owner.side_effect = None
                async with client.get(server.origin + '/api/sessions/archived') as response:
                    assert response.status == 200
                    assert (await response.json())['sessions'][0]['lifecycle'] == 'ARCHIVED'
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
        core._session_retirements = {}
        admitted = await core._admit_session_open("parent", "child")
        pending = asyncio.create_task(core.begin_session_retirement("child", "u_local"))
        await asyncio.sleep(0)
        assert not pending.done()
        with pytest.raises(SessionDeleteRejected):
            await core._admit_session_open("child")
        await core._settle_session_open(admitted)
        operation = await pending
        await core.finish_session_retirement(operation, quarantine=False)
        assert not core._open_attempts and not core._session_retirements

    asyncio.run(run())


def test_archived_delete_http_needs_no_chat_connection_and_preserves_public_results(tmp_path):
    from aiohttp import ClientSession
    from pulsara_agent.desktop_app.http_server import LocalHttpServer
    from pulsara_agent.desktop_app.application import packaged_static_root
    from tests.test_desktop_http_surface import _model_server_dependencies
    from pulsara_agent.desktop_app.session_controller import SessionControlRejected

    async def run():
        c, core, bridge, session, _ = control(tmp_path)
        bridge.require_chat_controller = AsyncMock(side_effect=AssertionError('cold deletion needs no controller'))
        draining = False
        server = LocalHttpServer(sessions=c, bridge=bridge, static_root=packaged_static_root(), requested_port=0,
            is_ready=lambda: True, is_draining=lambda: draining, **_model_server_dependencies())
        await server.start()
        try:
            async with ClientSession() as client:
                url = server.origin + '/api/sessions/' + session.session_id + '/archived'
                for body in ({}, {'confirm_permanent_delete': 1}, {'confirm_permanent_delete': False}, {'confirm_permanent_delete': True, 'extra': True}):
                    async with client.delete(url, json=body) as response:
                        assert response.status == 400
                core.begin_session_retirement.assert_not_awaited()
                for headers in ({'Origin': 'https://elsewhere.example'}, {'Sec-Fetch-Site': 'cross-site'}):
                    async with client.delete(url, json={'confirm_permanent_delete': True}, headers=headers) as response:
                        assert response.status == 403
                draining = True
                async with client.delete(url, json={'confirm_permanent_delete': True}) as response:
                    assert response.status == 503
                draining = False
                core.read_session_lifecycle.return_value = 'OPEN'
                core.finish_session_retirement.side_effect = RuntimeError('gate release failed')
                async with client.delete(url, json={'confirm_permanent_delete': True}) as response:
                    assert response.status == 200
                    payload = await response.json()
                    assert payload['status'] == 'NOT_ARCHIVED'
                    assert '重启 Pulsara' in payload['cleanup_warning']['message']
                core.quiesce_session_retirement.assert_not_awaited()
                c.delete_session = AsyncMock(side_effect=SessionControlRejected('SESSION_CONTROL_BUSY', '会话正在处理另一项操作。'))
                async with client.delete(url, json={'confirm_permanent_delete': True}) as response:
                    assert response.status == 409
                    assert (await response.json())['error']['code'] == 'SESSION_CONTROL_BUSY'
                bridge.require_chat_controller.assert_not_awaited()
        finally:
            await server.aclose()
    asyncio.run(run())


def test_core_commit_joins_physical_database_worker_even_after_waiter_cancel():
    from threading import Event
    from time import monotonic
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.conversation_kernel.session_deletion import KernelSessionRetirement

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
        op = KernelSessionRetirement(
            "target",
            "u_local",
            core,
            asyncio.get_running_loop().create_future(),
            physical_full=True,
        )
        core._session_retirements = {"target": op}
        pending = asyncio.create_task(core.commit_session_deletion(op))
        assert await asyncio.to_thread(entered.wait, 10)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        release.set()
        assert await pending == "DELETED"

    asyncio.run(run())
