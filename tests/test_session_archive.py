"""Idle-only archive, explicit restore and no implicit execution recovery."""

import asyncio
from dataclasses import replace
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock

import psycopg
import pytest

from pulsara_agent.conversation_kernel._repository.deletion import SessionDeletionBusy
from pulsara_agent.conversation_kernel.host import (
    KernelHostSession,
    _list_resumable_session_rows_across_workspaces,
)
from pulsara_agent.conversation_kernel.repository import ConversationKernelConflict
from pulsara_agent.conversation_kernel.session_deletion import SessionDeleteRejected
from tests.test_conversation_fork import repo as repo, new_session, turn, rows, identity
from tests.test_session_deletion_control import control
from tests.test_stage2_conversation_kernel_postgres import _enqueue_binding_candidate
from tests.support.model_config import (
    test_model_resolution_snapshot as model_snapshot,
    test_model_binding as model_binding,
    test_model_runtime as model_runtime,
)


def archive(repo, lease, **kwargs):
    return repo.archive_session(
        session_id=lease.guard.session_id,
        memory_domain_id="u_local",
        closed_writer=lease.guard,
        deadline_monotonic=monotonic() + 30,
        **kwargs,
    )


def test_archive_restore_is_explicit_idempotent_and_keeps_history(repo):
    lease = new_session(repo)
    turn(repo, lease.guard, "keep this conversation")
    sid = lease.guard.session_id
    before = rows(
        repo,
        "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s ORDER BY entry_sequence",
        (sid,),
    )
    assert archive(repo, lease, check_only=True) == "OPEN"
    assert archive(repo, lease) == "ARCHIVED"
    assert archive(repo, lease) == "ARCHIVED"
    summary = next(
        r
        for r in _list_resumable_session_rows_across_workspaces(
            repo, "u_local", True, monotonic() + 30
        )
        if r["id"] == sid
    )
    assert (
        summary["updated_at"]
        == rows(repo, "SELECT updated_at FROM pulsara_v3.sessions WHERE id=%s", (sid,))[
            0
        ]["updated_at"]
    )
    assert not any(
        r["id"] == sid
        for r in _list_resumable_session_rows_across_workspaces(
            repo, "u_local", False, monotonic() + 30
        )
    )
    assert (
        rows(
            repo,
            "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s ORDER BY entry_sequence",
            (sid,),
        )
        == before
    )
    with pytest.raises(ConversationKernelConflict, match="archived"):
        repo.acquire_host_writer(
            intent="EXISTING",
            session_id=sid,
            workspace_id=rows(
                repo, "SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s", (sid,)
            )[0]["workspace_id"],
            writer_owner_id="host:cannot-restore",
            lease_seconds=30,
            deadline_monotonic=monotonic() + 30,
        )
    for _ in range(2):
        assert (
            repo.unarchive_session(
                session_id=sid,
                memory_domain_id="u_local",
                deadline_monotonic=monotonic() + 30,
            )
            == "OPEN"
        )
    assert rows(
        repo,
        "SELECT lifecycle,writer_lease_owner_id FROM pulsara_v3.sessions WHERE id=%s",
        (sid,),
    ) == [{"lifecycle": "OPEN", "writer_lease_owner_id": None}]
    for method in (repo.archive_session, repo.unarchive_session):
        params = {"closed_writer": None} if method == repo.archive_session else {}
        with pytest.raises(KeyError):
            method(
                session_id="session:missing",
                memory_domain_id="u_local",
                deadline_monotonic=monotonic() + 30,
                **params,
            )


@pytest.mark.parametrize("work", ["turn", "queue"])
def test_canonical_unfinished_work_rejects_archive_without_changes(repo, work):
    lease = new_session(repo)
    if work == "turn":
        turn(repo, lease.guard, "busy", finish=False)
    else:
        repo.update_session_model_call_binding(
            lease.guard,
            binding=model_binding(model_runtime()),
            deadline_monotonic=monotonic() + 30,
        )
        _enqueue_binding_candidate(
            repo,
            lease.guard,
            cut=model_snapshot(),
            command_id=identity("command"),
            queue_item_id=identity("queue"),
            text=b"waiting",
        )
    with pytest.raises(SessionDeletionBusy, match="unfinished"):
        archive(repo, lease)
    assert (
        rows(
            repo,
            "SELECT lifecycle FROM pulsara_v3.sessions WHERE id=%s",
            (lease.guard.session_id,),
        )[0]["lifecycle"]
        == "OPEN"
    )


def test_cold_archive_checks_domain_live_lease_and_exact_writer(
    repo, stage2_migrated_postgres_database
):
    lease = new_session(repo)
    sid = lease.guard.session_id
    params = dict(
        session_id=sid, closed_writer=None, deadline_monotonic=monotonic() + 30
    )
    with pytest.raises(KeyError):
        repo.archive_session(memory_domain_id="other", **params)
    with pytest.raises(SessionDeletionBusy, match="live writer"):
        repo.archive_session(memory_domain_id="u_local", **params)
    with pytest.raises(SessionDeletionBusy, match="changed"):
        archive(
            repo,
            SimpleNamespace(
                guard=replace(
                    lease.guard, writer_generation=lease.guard.writer_generation + 1
                )
            ),
        )
    assert sid not in repo.idle_session_ids(
        memory_domain_id="u_local",
        local_session_ids=(),
        deadline_monotonic=monotonic() + 30,
    )
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as c:
        c.execute(
            "UPDATE pulsara_v3.sessions SET writer_lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (sid,),
        )
    assert sid in repo.idle_session_ids(
        memory_domain_id="u_local",
        local_session_ids=(),
        deadline_monotonic=monotonic() + 30,
    )
    assert repo.archive_session(memory_domain_id="u_local", **params) == "ARCHIVED"


@pytest.mark.parametrize("kind", ["process", "monitor", "pending", "idle"])
def test_archive_terminal_admission(kind):
    tools = SimpleNamespace(
        list_background_terminal_processes=lambda: (
            [SimpleNamespace(physical_state="RUNNING")] if kind == "process" else []
        ),
        terminal_monitor_coordinator=SimpleNamespace(
            list_current=lambda: ["monitor"] if kind == "monitor" else [],
            pending_monitor_ids=lambda: ["pending"] if kind == "pending" else [],
        ),
    )
    assert KernelHostSession.archive_has_terminal_work(
        SimpleNamespace(_tools=tools)
    ) is (kind != "idle")


def test_busy_archive_keeps_original_connection_and_runtime(tmp_path):
    async def run():
        c, core, bridge, session, connection = control(tmp_path)
        core.prepare_session_archive = AsyncMock(
            side_effect=SessionDeletionBusy("unfinished work")
        )
        core.commit_session_archive = AsyncMock()
        with pytest.raises(SessionDeleteRejected) as error:
            await c.archive_session(session.session_id, bridge=bridge)
        assert error.value.public_code == "SESSION_NOT_IDLE"
        core.quiesce_session_retirement.assert_not_awaited()
        core.commit_session_archive.assert_not_awaited()
        assert not connection.close_attempted
        assert session.session_id in c._by_session
        assert session.session_id not in c._operations
        assert not core.finish_session_retirement.await_args.kwargs["quarantine"]

    asyncio.run(run())


def test_archive_ambiguous_ack_confirms_only_after_waiter_loss(tmp_path):
    async def run():
        c, core, bridge, session, _ = control(tmp_path)
        core.prepare_session_archive = AsyncMock()
        entered, release = asyncio.Event(), asyncio.Event()

        async def commit(_):
            entered.set()
            await release.wait()
            raise OSError("ACK lost")

        core.commit_session_archive = AsyncMock(side_effect=commit)
        core.read_session_lifecycle = AsyncMock(
            side_effect=[OSError("offline"), "ARCHIVED"]
        )
        waiter = asyncio.create_task(
            c.archive_session(session.session_id, bridge=bridge)
        )
        await entered.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        with pytest.raises(SessionDeleteRejected) as error:
            await c.archive_session(session.session_id, bridge=bridge)
        assert error.value.public_code == "SESSION_ARCHIVE_UNCONFIRMED"
        assert bridge._session_locks[session.session_id].locked()
        assert (await c.archive_session(session.session_id, bridge=bridge))[
            "status"
        ] == "ARCHIVED"
        core.commit_session_archive.assert_awaited_once()
        core.commit_session_deletion.assert_not_awaited()
        assert session.session_id not in c._by_session
        assert not bridge._session_locks

    asyncio.run(run())


def test_unarchive_uncertain_retry_confirms_without_second_write(tmp_path):
    async def run():
        c, core, _, session, _ = control(tmp_path)
        core.unarchive_session = AsyncMock(side_effect=OSError("ACK lost"))
        core.read_session_lifecycle = AsyncMock(
            side_effect=[OSError("offline"), "OPEN"]
        )
        with pytest.raises(SessionDeleteRejected) as error:
            await c.unarchive_session(session.session_id)
        assert error.value.status == 503
        assert session.session_id in c._operations
        assert (await c.unarchive_session(session.session_id))["status"] == "OPEN"
        core.unarchive_session.assert_awaited_once()
        core.begin_session_retirement.assert_not_awaited()
        assert session.session_id not in c._operations

    asyncio.run(run())
