"""Idle unload uses the production Host, writer and work admission owners."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import shlex
from threading import Event

import pytest

from pulsara_agent.conversation_kernel.host_contracts import (
    HostSessionCloseState, KernelSessionSettlementUnconfirmed, SessionControlRejected,
)
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.primitives.permission import PermissionMode
from tests.test_agent_session_host import AgentModel
from tests.test_round4_plan_host import _DetachedDraftModel, _open_test_session, _text, _tool_call
from tests.test_stage2_kernel_host_dogfood import _SteerModelPort


pytestmark = pytest.mark.postgres


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    root = tmp_path / "workspace"
    root.mkdir()
    return root


async def _open(workspace, database, monkeypatch, model):
    return await _open_test_session(
        tmp_path=workspace, model=model, monkeypatch=monkeypatch, postgres_dsn=database.runtime_dsn,
    )


def _scheduled_request(session, task):
    return dict(client_command_id=random_id("command"), session_id=session.session_id,
                task_id=task["id"], expected_revision=task["revision"],
                request_at_utc=datetime.now(timezone.utc).replace(microsecond=0).isoformat())


async def _wait_for_admission(event, request):
    waiter = asyncio.create_task(event.wait())
    try:
        settled, _ = await asyncio.wait((waiter, request), timeout=5,
                                        return_when=asyncio.FIRST_COMPLETED)
        if request in settled:
            request.result()
            pytest.fail("scheduled request completed before the admission gate")
        assert waiter in settled
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.parametrize("winner", ["work", "unload"])
def test_scheduled_work_and_exact_idle_unload_share_one_admission_cut(
    workspace, monkeypatch, stage2_migrated_postgres_database, winner,
):
    async def exercise():
        model = _SteerModelPort()
        core, session = await _open(workspace, stage2_migrated_postgres_database, monkeypatch, model)
        entered, release = asyncio.Event(), asyncio.Event()
        request = None
        try:
            service = core.scheduled_tasks
            tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat()
            task = await service.create(
                session_id=session.session_id, status="ACTIVE", values=dict(
                    name="Scheduled work", prompt="Run one assignment", timezone="UTC",
                    permission_mode="ask-permissions",
                    schedule={"kind": "interval", "anchor_at_utc": tomorrow, "seconds": 900},
                ),
            )
            if winner == "work":
                previous_resume = core.resume_scheduled_session

                async def hold_resume(session_id):
                    entered.set()
                    await release.wait()
                    return await previous_resume(session_id)

                monkeypatch.setattr(core, "resume_scheduled_session", hold_resume)
                request = asyncio.create_task(service.run_now(_scheduled_request(session, task)))
                await _wait_for_admission(entered, request)
                assert await core.try_unload_idle_session(
                    session.host_session_id, can_close=lambda: pytest.fail("work already won"),
                ) is None
            else:
                original_close = session.aclose

                async def hold_close(**kwargs):
                    entered.set()
                    await release.wait()
                    await original_close(**kwargs)

                monkeypatch.setattr(session, "aclose", hold_close)
                attempt = await core.try_unload_idle_session(session.host_session_id, can_close=lambda: True)
                assert attempt is not None
                await asyncio.wait_for(entered.wait(), 5)
                joining_admission = asyncio.Event()
                previous_admission = core.scheduled_session_admission

                @asynccontextmanager
                async def observe_admission(session_id):
                    joining_admission.set()
                    async with previous_admission(session_id):
                        yield

                monkeypatch.setattr(core, "scheduled_session_admission", observe_admission)
                request = asyncio.create_task(service.run_now(_scheduled_request(session, task)))
                await _wait_for_admission(joining_admission, request)
                assert not request.done()
                assert not model.requests
                assert core.session_runtime_closing(session.session_id)
            release.set()
            assert (await asyncio.wait_for(request, 10))["queue_item_id"]
            await asyncio.wait_for(model.started.wait(), 5)
            current = await core.resume_scheduled_session(session.session_id)
            if winner == "unload":
                await asyncio.shield(attempt.task)
                assert attempt.state is HostSessionCloseState.CLOSED
                assert current.host_session_id != session.host_session_id
                assert current._lease.guard.writer_generation > session._lease.guard.writer_generation
            else:
                assert current is session
                assert await core.try_unload_idle_session(
                    session.host_session_id, can_close=lambda: pytest.fail("active ROOT already won"),
                ) is None
            assert (await service.get(task["id"]))["status"] == "ACTIVE"
            assert await core.read_session_lifecycle(session.session_id, "u_local") == "OPEN"
        finally:
            release.set()
            model.release.set()
            if request is not None:
                await asyncio.gather(request, return_exceptions=True)
            await core.shutdown()

    asyncio.run(exercise())


def test_core_skips_idle_unload_if_connection_wins_final_check(
    workspace, monkeypatch, stage2_migrated_postgres_database,
):
    async def exercise():
        core, session = await _open(workspace, stage2_migrated_postgres_database, monkeypatch, _SteerModelPort())
        entered, release = asyncio.Event(), Event()
        loop = asyncio.get_running_loop()
        original_check = session.repository.archive_session
        allow_close = True
        callback_calls = []

        def check(**kwargs):
            assert kwargs["check_only"]
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
            return original_check(**kwargs)

        def can_close():
            callback_calls.append(allow_close)
            return allow_close

        monkeypatch.setattr(session.repository, "archive_session", check)
        checking = asyncio.create_task(core.try_unload_idle_session(session.host_session_id, can_close=can_close))
        joined = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert not callback_calls
            allow_close = False
            joined = asyncio.create_task(core.wait_for_idle_unload(session.session_id))
            await asyncio.sleep(0)
            assert not joined.done()
            release.set()
            assert await asyncio.wait_for(checking, 5) is None
            await asyncio.wait_for(joined, 5)
            assert callback_calls == [False]
            assert session._runtime_reopen_quiescence is None
            assert await session.attach_controller(random_id("attachment"))
            assert await core.resume_scheduled_session(session.session_id) is session
        finally:
            release.set()
            await asyncio.gather(checking, *(task for task in (joined,) if task), return_exceptions=True)
            await core.shutdown()

    asyncio.run(exercise())


def test_idle_unload_preserves_writer_release_failure(
    workspace, monkeypatch, stage2_migrated_postgres_database,
):
    async def exercise():
        core, session = await _open(workspace, stage2_migrated_postgres_database, monkeypatch, _SteerModelPort())
        original_release = session.repository.release_host_writer

        def fail_release(guard, **kwargs):
            if guard == session._lease.guard:
                raise RuntimeError("lost release response")
            return original_release(guard, **kwargs)

        monkeypatch.setattr(session.repository, "release_host_writer", fail_release)
        try:
            attempt = await core.try_unload_idle_session(session.host_session_id, can_close=lambda: True)
            assert attempt is not None
            with pytest.raises(KernelSessionSettlementUnconfirmed):
                await asyncio.shield(attempt.task)
            assert session.physical_close_complete
            assert attempt.state is HostSessionCloseState.CLOSED_SETTLEMENT_UNCONFIRMED
            assert core.session_runtime_closing(session.session_id)
            with pytest.raises(SessionControlRejected) as rejected:
                await core.resume_scheduled_session(session.session_id)
            assert rejected.value.public_code == "SESSION_RECOVERY_CLOSE_FAILED"
            assert await core.read_session_lifecycle(session.session_id, "u_local") == "OPEN"
        finally:
            with pytest.raises(KernelSessionSettlementUnconfirmed):
                await core.shutdown()

    asyncio.run(exercise())


def test_detached_terminal_process_prevents_idle_unload_until_physical_exit(
    workspace, monkeypatch, stage2_migrated_postgres_database,
):
    exit_file = workspace / "exit-terminal"

    class Model(_DetachedDraftModel):
        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                events = _tool_call("exec_process", "call:terminal", {
                    "command": f"while [ ! -e {shlex.quote(str(exit_file))} ]; do sleep 0.05; done",
                    "yield_time_ms": 0,
                })
            else:
                events = _text("Detached")
            for event in events:
                yield event

    async def exercise():
        core, session = await _open(workspace, stage2_migrated_postgres_database, monkeypatch, Model())
        try:
            await asyncio.wait_for(session.run_turn(PromptContent.text("Start the local test process")), 10)
            assert session.archive_has_terminal_work()
            assert await core.try_unload_idle_session(
                session.host_session_id, can_close=lambda: pytest.fail("terminal still running"),
            ) is None
            assert session.archive_has_terminal_work()
            exit_file.touch()
            async with asyncio.timeout(5):
                while session.archive_has_terminal_work():
                    await asyncio.sleep(0.01)
            attempt = await core.try_unload_idle_session(session.host_session_id, can_close=lambda: True)
            assert attempt is not None
            await asyncio.shield(attempt.task)
            assert attempt.state is HostSessionCloseState.CLOSED
        finally:
            exit_file.touch()
            await core.shutdown()

    asyncio.run(exercise())


def test_root_idle_unload_preserves_canonical_and_running_child_work(
    workspace, monkeypatch, stage2_migrated_postgres_database,
):
    async def exercise():
        model = AgentModel()
        core, root = await _open(workspace, stage2_migrated_postgres_database, monkeypatch, model)
        model.root_id = root.session_id
        slot = random_id("session")
        await core._agent_runtime.set_capacity_target(1)
        assert await core._agent_runtime.try_acquire(slot)
        try:
            await asyncio.wait_for(root.run_turn(
                PromptContent.text("Delegate a test assignment"),
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            ), 15)
            (child_id,) = await root._io.run(
                root.repository.list_root_agent_session_ids,
                root_session_id=root.session_id, deadline_monotonic=root._canonical_deadline(),
            )

            async def work_status():
                (work,) = await root._io.run(root.repository.list_agent_works, root_session_id=root.session_id,
                    agent_id=child_id, deadline_monotonic=root._canonical_deadline())
                return work["status"]

            assert await work_status() == "queued"
            assert core._live_agent_session(child_id) is None
            assert await core.try_unload_idle_session(
                root.host_session_id, can_close=lambda: pytest.fail("canonical child work is queued"),
            ) is None
            assert await work_status() == "queued"
            await core._agent_runtime.release(slot)
            await asyncio.wait_for(model.first_child_started.wait(), 10)
            child = core._live_agent_session(child_id)
            assert child is not None
            assert await core.try_unload_idle_session(
                root.host_session_id, can_close=lambda: pytest.fail("child work is running"),
            ) is None
            assert await work_status() == "running"
            model.first_child_release.set()
            async with asyncio.timeout(10):
                while await work_status() == "running":
                    await asyncio.sleep(0.01)
                assert await work_status() == "completed"
                while (attempt := await core.try_unload_idle_session(root.host_session_id, can_close=lambda: True)) is None:
                    await asyncio.sleep(0.01)
            await asyncio.shield(attempt.task)
            assert root.physical_close_complete and child.physical_close_complete
            assert await core.read_session_lifecycle(root.session_id, "u_local") == "OPEN"
        finally:
            await core._agent_runtime.release(slot)
            model.first_child_release.set()
            await core.shutdown()

    asyncio.run(exercise())
