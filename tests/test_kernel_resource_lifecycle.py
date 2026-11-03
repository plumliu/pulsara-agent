"""Core admission owns cold resources and the physical lifetime of cold reads."""

import asyncio
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pulsara_agent.conversation_kernel import host
from pulsara_agent.workspace_identity import HostWorkspaceInput
from pulsara_agent.identity import random_id
from tests.support.model_config import test_model_runtime as make_runtime


class Lease:
    def __init__(self):
        self.connection_provider = object()
        self.released = False

    def release(self):
        assert not self.released
        self.released = True


class History:
    physical_operation_deadline = None
    def __init__(self, _provider):
        self.physical_close_complete = False

    async def aclose(self, **_kwargs):
        self.physical_close_complete = True


def resources(monkeypatch):
    core = host.KernelHostCore.production(model_runtime=make_runtime(postgres_dsn="postgresql://unused/unused"))
    core.start_scheduled_tasks = AsyncMock()
    leases = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def acquire(*_args, **_kwargs):
        lease = Lease()
        leases.append(lease)
        entered.set()
        await release.wait()
        return lease

    monkeypatch.setattr(host, "process_postgres_schema_verification_service", lambda: SimpleNamespace(acquire=acquire))
    monkeypatch.setattr(host, "require_stage2_runtime_privilege_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(host, "ConversationKernelRepository", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(host, "PostgresCanonicalBlobStore", lambda *_args: SimpleNamespace())
    monkeypatch.setattr(host, "HistoryProjectionMaintainer", History)

    async def gc():
        await asyncio.Event().wait()

    monkeypatch.setattr(core, "_blob_gc_loop", gc)
    return core, leases, entered, release


def test_cold_public_preparation_reads_and_opens_share_one_operation(monkeypatch, tmp_path):
    async def run():
        core, leases, entered, release = resources(monkeypatch)
        monkeypatch.setattr(host, "_list_resumable_session_rows_across_workspaces", lambda *_args: [])

        async def opened(*_args, **_kwargs):
            return await core._ensure_resources()

        monkeypatch.setattr(core, "_open_admitted", opened)
        first = asyncio.create_task(core.prepare_resources())
        await entered.wait()
        second = asyncio.create_task(core.prepare_resources())
        read = asyncio.create_task(core.list_resumable_sessions_across_workspaces())
        opened_task = asyncio.create_task(core.open_session(HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path)))
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not leases[0].released
        release.set()
        await asyncio.gather(second, read, opened_task)
        assert len(leases) == 1
        assert core._resource_initialization.cleanup_complete
        assert core._blob_gc_task is not None
        # Preparing resources does not start the Agent execution scheduler.
        assert core._agent_runtime._task is None
        await core.shutdown()
        assert leases[0].released
        assert core._blob_gc_task is None and core._blob_gc_io is None

    asyncio.run(run())


def test_shutdown_joins_cold_initialization_and_prevents_closed_reactivation(monkeypatch, tmp_path):
    async def run():
        core, leases, entered, release = resources(monkeypatch)
        prepare = asyncio.create_task(core.prepare_resources())
        await entered.wait()
        stopped = asyncio.Event()
        original_stop = core.scheduled_tasks.stop

        async def stop():
            stopped.set()
            await original_stop()

        core.scheduled_tasks.stop = stop
        closing = asyncio.create_task(core.shutdown())
        await stopped.wait()
        assert not closing.done() and not leases[0].released
        release.set()
        with pytest.raises(host.KernelHostCoreClosing):
            await prepare
        await closing
        assert leases[0].released and core._closed
        for operation in (core.prepare_resources, core.list_resumable_sessions_across_workspaces,
                          lambda: core.open_session(HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path))):
            with pytest.raises(host.KernelHostCoreClosing):
                await operation()
        assert len(leases) == 1
        await core.shutdown()

    asyncio.run(run())


def test_failed_initialization_retries_only_after_complete_cleanup(monkeypatch):
    async def run():
        core, leases, _entered, release = resources(monkeypatch)
        release.set()
        attempts = 0

        def repository(*_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("constructor failed")
            return SimpleNamespace()

        monkeypatch.setattr(host, "ConversationKernelRepository", repository)
        with pytest.raises(OSError, match="constructor failed"):
            await core.prepare_resources()
        assert leases[0].released and core._resource_initialization.cleanup_complete
        assert core._repository is None
        await core.prepare_resources()
        assert len(leases) == 2 and not leases[1].released
        await core.shutdown()
        assert leases[1].released

    asyncio.run(run())


def test_unconfirmed_candidate_cleanup_retains_lease_and_forbids_retry(monkeypatch):
    async def run():
        core, leases, _entered, release = resources(monkeypatch)
        release.set()

        class UnstoppedHistory(History):
            async def aclose(self, **_kwargs):
                raise OSError("physical history cleanup unconfirmed")

        monkeypatch.setattr(host, "HistoryProjectionMaintainer", UnstoppedHistory)
        monkeypatch.setattr(host, "KernelSessionIO", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("IO construction failed")))
        for _ in range(2):
            with pytest.raises(RuntimeError, match="IO construction failed"):
                await core.prepare_resources()
        initialization = core._resource_initialization
        assert len(leases) == 1 and not leases[0].released
        assert initialization.access is leases[0]
        assert initialization.history is not None and not initialization.cleanup_complete
        assert core._repository is None and core.has_database_resources
        with pytest.raises(RuntimeError, match="physical close is unconfirmed"):
            await core.shutdown()
        assert not leases[0].released and not core._closed
        # The fake owns no worker; manually release only after the assertions.
        leases[0].release()

    asyncio.run(run())


def test_shutdown_waits_for_accepted_physical_read(monkeypatch):
    async def run():
        core, leases, _entered, release = resources(monkeypatch)
        release.set()
        await core.prepare_resources()
        reading, finish = Event(), Event()

        def rows(*_args):
            reading.set()
            assert finish.wait(5)
            return []

        monkeypatch.setattr(host, "_list_resumable_session_rows_across_workspaces", rows)
        read = asyncio.create_task(core.list_resumable_sessions_across_workspaces())
        assert await asyncio.to_thread(reading.wait, 5)
        stopped = asyncio.Event()
        original_stop = core.scheduled_tasks.stop

        async def stop():
            stopped.set()
            await original_stop()

        core.scheduled_tasks.stop = stop
        closing = asyncio.create_task(core.shutdown())
        await stopped.wait()
        assert not closing.done() and not leases[0].released
        finish.set()
        assert await read == []
        await closing
        assert leases[0].released and core._closed

    asyncio.run(run())


@pytest.mark.parametrize("stopped,settled", [(False, False), (True, True), (True, False)])
def test_independent_session_close_failures_keep_only_necessary_dependencies(monkeypatch, stopped, settled):
    async def run():
        core, leases, _entered, release = resources(monkeypatch)
        release.set()
        await core.prepare_resources()
        failure = OSError("session A close failure")

        def session():
            return SimpleNamespace(
                session_id=random_id("session"), host_session_id=random_id("host"),
                root_session_id=None, physical_close_complete=False,
                necessary_settlement_complete=False, local_children_stopped=True,
                _stop_close_admission=AsyncMock(), _lease=SimpleNamespace(guard=object()),
            )

        first, second = session(), session()

        async def close_first(**_kwargs):
            first.physical_close_complete = stopped
            first.necessary_settlement_complete = settled
            raise failure

        async def close_second(**_kwargs):
            second.physical_close_complete = second.necessary_settlement_complete = True

        first.aclose = AsyncMock(side_effect=close_first)
        second.aclose = AsyncMock(side_effect=close_second)
        core._sessions = {item.host_session_id: item for item in (first, second)}
        core._repository.release_host_writer = lambda *_args, **_kwargs: None
        if not stopped:
            with pytest.raises(OSError) as observed:
                await core.shutdown()
            assert observed.value is failure
            assert not core._closed and not leases[0].released
            assert core._close_attempts[first.host_session_id].state is host.HostSessionCloseState.CLOSE_FAILED_QUARANTINED
            assert core.session_runtime_closing(first.session_id)
        elif not settled:
            with pytest.raises(host.KernelSessionSettlementUnconfirmed):
                await core.shutdown()
            assert core._closed and leases[0].released
            assert not core._close_attempts and not core._sessions
        else:
            await core.shutdown()
            assert core._closed and leases[0].released
            assert not core._close_attempts and not core._sessions
        second.aclose.assert_awaited_once()
        assert second.host_session_id not in core._sessions
        if not stopped:
            leases[0].release()

    asyncio.run(run())


def test_blocked_session_close_does_not_delay_an_independent_session(monkeypatch):
    async def run():
        core, leases, _entered, release = resources(monkeypatch)
        release.set()
        await core.prepare_resources()
        finish_first, second_closed = asyncio.Event(), asyncio.Event()
        first = SimpleNamespace(session_id=random_id("session"), host_session_id=random_id("host"),
            root_session_id=None, physical_close_complete=False, necessary_settlement_complete=False,
            _stop_close_admission=AsyncMock(), _lease=SimpleNamespace(guard=object()))
        second = SimpleNamespace(session_id=random_id("session"), host_session_id=random_id("host"),
            root_session_id=None, physical_close_complete=False, necessary_settlement_complete=False,
            _stop_close_admission=AsyncMock(), _lease=SimpleNamespace(guard=object()))

        async def close_first(**_kwargs):
            await finish_first.wait()
            first.physical_close_complete = first.necessary_settlement_complete = True

        async def close_second(**_kwargs):
            second.physical_close_complete = second.necessary_settlement_complete = True
            second_closed.set()

        first.aclose, second.aclose = close_first, close_second
        core._sessions = {item.host_session_id: item for item in (first, second)}
        core._repository.release_host_writer = lambda *_args, **_kwargs: None
        shutdown = asyncio.create_task(core.shutdown())
        await second_closed.wait()
        assert not shutdown.done() and not leases[0].released
        finish_first.set()
        await shutdown
        assert leases[0].released

    asyncio.run(run())


def test_shared_stop_join_does_not_block_installing_session_close(monkeypatch):
    async def run():
        core, leases, _entered, release = resources(monkeypatch)
        release.set()
        await core.prepare_resources()
        finish_stop, session_closed = asyncio.Event(), asyncio.Event()
        original_stop = core.scheduled_tasks.stop

        async def stop():
            await finish_stop.wait()
            await original_stop()

        core.scheduled_tasks.stop = stop
        session = SimpleNamespace(session_id=random_id("session"), host_session_id=random_id("host"),
            root_session_id=None, physical_close_complete=False, necessary_settlement_complete=False,
            _stop_close_admission=AsyncMock(), _lease=SimpleNamespace(guard=object()))

        async def close_session(**_kwargs):
            session.physical_close_complete = session.necessary_settlement_complete = True
            session_closed.set()

        session.aclose = close_session
        core._sessions[session.host_session_id] = session
        core._repository.release_host_writer = lambda *_args, **_kwargs: None
        closing = asyncio.create_task(core.shutdown())
        await asyncio.wait_for(session_closed.wait(), 2)
        assert session._closing
        assert not closing.done() and not leases[0].released
        finish_stop.set()
        await closing
        assert leases[0].released

    asyncio.run(run())


@pytest.mark.parametrize("initializing", [False, True])
def test_workbench_boundary_uses_the_actual_cold_operation_deadline_without_session(monkeypatch, initializing):
    async def run():
        core, leases, entered, release = resources(monkeypatch)
        finish = Event()
        reading = Event()
        if initializing:
            operation = asyncio.create_task(core.prepare_resources())
            await entered.wait()
            original_deadline = core._resource_initialization.deadline_monotonic
        else:
            release.set()
            await core.prepare_resources()

            def rows(*_args):
                reading.set()
                assert finish.wait(5)
                return []

            monkeypatch.setattr(host, "_list_resumable_session_rows_across_workspaces", rows)
            operation = asyncio.create_task(core.list_resumable_sessions_across_workspaces())
            assert await asyncio.to_thread(reading.wait, 5)
            original_deadline = next(iter(core._resource_operations.values()))
        image_closed = asyncio.Event()
        original_close = core._image_validator.aclose

        async def close_image():
            await original_close()
            image_closed.set()

        core._image_validator.aclose = close_image
        closing = asyncio.create_task(core.shutdown())
        await image_closed.wait()
        assert core.workbench_exit_deadline == original_deadline
        assert not core.workbench_exit_ready
        monkeypatch.setattr(host, "monotonic", lambda: original_deadline + 1)
        assert core.workbench_exit_ready
        assert not closing.done() and not leases[0].released
        release.set()
        finish.set()
        if initializing:
            with pytest.raises(host.KernelHostCoreClosing):
                await operation
        else:
            assert await operation == []
        await closing
        assert leases[0].released

    asyncio.run(run())


def test_accepted_fork_database_reservation_is_not_a_live_child_process(monkeypatch, tmp_path):
    async def run():
        core, leases, _entered, release = resources(monkeypatch)
        release.set()
        await core.prepare_resources()
        reading, finish = Event(), Event()
        calls = 0

        def fork(**_kwargs):
            nonlocal calls
            calls += 1
            reading.set()
            assert finish.wait(5)
            return SimpleNamespace(created=False)

        core._repository.fork_conversation = fork
        core.read_resumable_session = AsyncMock(return_value=SimpleNamespace(workspace_root=tmp_path))
        operation = asyncio.create_task(core.fork_conversation(
            source_session_id=random_id('session'), child_session_id=random_id('session'),
            anchor_entry_id=random_id('entry'), memory_domain_id='u_local'))
        assert await asyncio.to_thread(reading.wait, 5)
        image_closed = asyncio.Event()
        original_close = core._image_validator.aclose

        async def close_image():
            await original_close()
            image_closed.set()

        core._image_validator.aclose = close_image
        closing = asyncio.create_task(core.shutdown())
        await image_closed.wait()
        assert core._open_attempts and all(attempt.configuration is None for attempt in core._open_attempts.values())
        original_deadline = core.workbench_exit_deadline
        monkeypatch.setattr(host, 'monotonic', lambda: original_deadline + 1)
        assert core.workbench_exit_ready
        assert not closing.done() and not leases[0].released
        finish.set()
        assert not (await operation).created
        await closing
        assert calls == 1 and leases[0].released

    asyncio.run(run())


def test_closed_sessionless_core_allows_discarding_an_application_warmup_wait(monkeypatch):
    from pulsara_agent.desktop_app.application import DesktopApplication
    from pulsara_agent.tool_permission import default_permission_policy

    async def run():
        core, _leases, _entered, _release = resources(monkeypatch)
        application = DesktopApplication(core=core, permission_policy=default_permission_policy())
        release_warmup = asyncio.Event()
        application._sdk_warmup_task = asyncio.create_task(release_warmup.wait())
        assert core.workbench_exit_deadline is None
        assert not core.workbench_exit_ready
        closing = asyncio.create_task(application.aclose())
        await core.shutdown()
        try:
            assert core._closed and not core._sessions and not core._close_attempts
            assert core.workbench_exit_deadline is None
            assert not closing.done() and not application._sdk_warmup_task.done()
            assert core.workbench_exit_ready and application.workbench_exit_ready
        finally:
            release_warmup.set()
            await closing

    asyncio.run(run())
