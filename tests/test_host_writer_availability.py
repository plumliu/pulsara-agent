"""A failed renewal seals admission and joins the existing Core close owner."""

import asyncio
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
)
from pulsara_agent.conversation_kernel.host import (
    HostSessionCloseState,
    KernelHostCore,
    KernelHostSession,
)
from pulsara_agent.conversation_kernel.repository import StaleHostWriter


@pytest.mark.parametrize('failure', [StaleHostWriter('expired'), OSError('connection lost')])
def test_failed_renewal_seals_admission_without_reviving_the_guard(monkeypatch, failure):
    async def scenario():
        guard = object()
        lease = SimpleNamespace(guard=guard)

        class IO:
            async def run(self, *_args, **_kwargs):
                raise failure

        session = object.__new__(KernelHostSession)
        session._writer_renewal_failure = None
        session._lease = lease
        session._memory_domain_id = 'u_local'
        session.repository = SimpleNamespace(renew_host_writer=lambda: None)
        session._io = IO()
        session._deadlines = KernelExecutionDeadlineFactory()
        real_sleep = asyncio.sleep

        async def ready(_delay):
            await real_sleep(0)

        monkeypatch.setattr(asyncio, 'sleep', ready)
        with pytest.raises(type(failure)) as observed:
            await session._renew_writer()
        assert observed.value is failure
        assert session._lease is lease and session._lease.guard is guard
        assert session.writer_unavailable
        assert session.writer_lost is isinstance(failure, StaleHostWriter)
        with pytest.raises(RuntimeError) as rejected:
            session._require_open()
        if not session.writer_lost:
            assert rejected.value.__cause__ is failure

    asyncio.run(scenario())


@pytest.mark.parametrize('close_failure', [None, OSError('SDK physical join failed')])
def test_renewal_done_callback_uses_the_original_close_attempt(close_failure):
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def close(*, deadline_monotonic):
            nonlocal calls
            assert deadline_monotonic > 0
            calls += 1
            entered.set()
            await release.wait()
            if close_failure is not None:
                raise close_failure

        async def failed_renewal():
            raise OSError('renewal connection lost')

        session = SimpleNamespace(
            host_session_id='host:writer-unavailable', session_id='session:writer-unavailable',
            writer_unavailable=True, aclose=close, _lease=SimpleNamespace(guard=object()),
        )
        core = object.__new__(KernelHostCore)
        core._lock = asyncio.Lock()
        core._deadlines = KernelExecutionDeadlineFactory()
        core._sessions = {session.host_session_id: session}
        core._close_attempts = {}
        core._extension_routes = {}
        core._repository = SimpleNamespace(release_host_writer=lambda *_args, **_kwargs: None)
        renewal = asyncio.create_task(failed_renewal())
        await asyncio.gather(renewal, return_exceptions=True)
        core._close_unavailable_writer(session, renewal)
        await entered.wait()
        attempt = core._close_attempts[session.host_session_id]
        another = asyncio.create_task(core.close_session(session.host_session_id))
        release.set()
        if close_failure is None:
            await another
            assert not core._close_attempts and not core._sessions
        else:
            with pytest.raises(OSError) as observed:
                await another
            assert observed.value is close_failure
            assert attempt.state is HostSessionCloseState.CLOSE_FAILED_QUARANTINED
            with pytest.raises(OSError) as repeated:
                await core.close_session(session.host_session_id)
            assert repeated.value is close_failure
        assert calls == 1

    asyncio.run(scenario())


def test_normal_renewal_cancellation_does_not_start_another_close():
    async def scenario():
        core = object.__new__(KernelHostCore)
        session = SimpleNamespace(writer_unavailable=False)
        renewal = asyncio.create_task(asyncio.sleep(100))
        renewal.cancel()
        await asyncio.gather(renewal, return_exceptions=True)
        core._close_unavailable_writer(session, renewal)

    asyncio.run(scenario())


@pytest.mark.postgres
def test_expired_writer_cannot_write_but_can_join_physical_close(
    isolated_migrated_postgres_database, tmp_path, monkeypatch,
):
    import psycopg
    from tests.support.model_config import test_model_runtime
    from pulsara_agent.workspace_identity import HostWorkspaceInput

    database = isolated_migrated_postgres_database
    monkeypatch.setenv('PULSARA_HOME', str(tmp_path / 'home'))
    workspace = tmp_path / 'workspace'
    workspace.mkdir()

    async def scenario():
        core = KernelHostCore.production(model_runtime=test_model_runtime(postgres_dsn=database.runtime_dsn))
        session = await core.open_session(HostWorkspaceInput(workspace_kind='project', workspace_root=workspace))
        guard = session._lease.guard
        with psycopg.connect(database.admin_dsn) as connection:
            row = connection.execute(
                "UPDATE pulsara_v3.sessions SET writer_lease_expires_at=clock_timestamp()-interval '1 second' "
                "WHERE id=%s AND writer_generation=%s AND writer_lease_owner_id=%s RETURNING id",
                (guard.session_id, guard.writer_generation, guard.writer_owner_id),
            ).fetchone()
            assert row is not None
        with pytest.raises(StaleHostWriter):
            await session._io.run(
                session.repository.read_session_model_call_binding, guard,
                deadline_monotonic=core._canonical_deadline(),
            )
        await core.close_session(session.host_session_id)
        assert session._closed
        assert session.host_session_id not in core._sessions
        assert not core._close_attempts
        assert session._lease.guard == guard
        await core.shutdown()

    asyncio.run(scenario())


@pytest.mark.postgres
@pytest.mark.parametrize('operation', ['read', 'fence', 'dispatch'])
def test_session_end_only_skips_the_exact_stale_guard_read(
    isolated_migrated_postgres_database, tmp_path, monkeypatch, operation,
):
    from unittest.mock import AsyncMock, patch
    from tests.support.model_config import test_model_runtime
    from pulsara_agent.workspace_identity import HostWorkspaceInput

    database = isolated_migrated_postgres_database
    monkeypatch.setenv('PULSARA_HOME', str(tmp_path / 'home'))
    workspace = tmp_path / 'workspace'
    workspace.mkdir()

    async def scenario():
        core = KernelHostCore.production(model_runtime=test_model_runtime(postgres_dsn=database.runtime_dsn))
        session = await core.open_session(HostWorkspaceInput(workspace_kind='project', workspace_root=workspace))
        failure = OSError('binding database failed') if operation == 'read' else StaleHostWriter('hook operation failed')
        if operation == 'read':
            target = patch.object(session.repository, 'read_session_model_call_binding', side_effect=failure)
        else:
            target = patch.object(session._hooks, 'fence_ordinary' if operation == 'fence' else 'dispatch_terminal', AsyncMock(side_effect=failure))
        with target:
            with pytest.raises(type(failure)) as first:
                await core.close_session(session.host_session_id)
        assert first.value is failure
        assert session._closed
        assert core._close_attempts[session.host_session_id].state is HostSessionCloseState.CLOSE_FAILED_QUARANTINED
        with pytest.raises(type(failure)) as repeated:
            await core.close_session(session.host_session_id)
        assert repeated.value is failure
        with pytest.raises(type(failure)) as shutdown:
            await core.shutdown()
        assert shutdown.value is failure

    asyncio.run(scenario())
