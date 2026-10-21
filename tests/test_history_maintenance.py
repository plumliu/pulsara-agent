from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from pulsara_agent.conversation_kernel import history_maintenance as maintenance
from pulsara_agent.conversation_kernel.history_publication import (
    HistoryProjectionPage,
    HistoryQuestionKey,
)
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.vocabulary import SubjectSlot
from tests.support.model_config import test_model_runtime


async def eventually(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


def test_fixed_running_progress_publishes_then_requeues_new_hint_and_other_question(
    monkeypatch,
):
    a, b = HistoryQuestionKey("s", "a"), HistoryQuestionKey("s", "b")
    entered, release = threading.Event(), threading.Event()
    calls, resolutions = [], []
    progress = {"a": 100, "b": 1}

    class Store:
        def __init__(self, provider):
            pass

        def scan_history_questions(self, after, width, **kwargs):
            return HistoryProjectionPage((), after, True)

        def resolve_history_questions(self, sid, ids, **kwargs):
            resolutions.append(ids)
            return tuple(dict.fromkeys(b if i == "b" else a for i in ids))

        def maintain_question(self, sid, qid, **kwargs):
            captured = progress[qid]
            if not entered.is_set():
                entered.set()
                assert release.wait(3)
            calls.append((qid, captured))
            return "published"

    monkeypatch.setattr(maintenance, "HistoryProjectionRepository", Store)
    monkeypatch.setattr(maintenance, "HISTORY_COALESCE_SECONDS", 0)

    async def run():
        owner = maintenance.HistoryProjectionMaintainer(object())
        try:
            owner.offer_entries("s", ("a1", "a2"))
            assert await asyncio.to_thread(entered.wait, 3)
            progress["a"] = 120
            owner.offer_entries("s", ("a3", "b"))
            release.set()
            await eventually(lambda: len(calls) >= 3)
            assert calls[0] == ("a", 100)
            assert ("a", 120) in calls and ("b", 1) in calls
            assert resolutions[0] == ("a1", "a2")
        finally:
            release.set()
            await owner.aclose(deadline_monotonic=maintenance.monotonic() + 3)

    asyncio.run(run())


def test_scanner_consumes_one_page_and_progresses_alongside_hot_live_question(
    monkeypatch,
):
    calls = []
    owner_slot = []

    class Store:
        def __init__(self, provider):
            pass

        def resolve_history_questions(self, sid, ids, **kwargs):
            return (HistoryQuestionKey(sid, "hot"),)

        def scan_history_questions(self, after, width, **kwargs):
            calls.append(("page", after))
            if not after:
                return HistoryProjectionPage(
                    (HistoryQuestionKey("s", "scan1"),), "cursor1", False
                )
            assert ("maintain", "scan1") in calls
            return HistoryProjectionPage(
                (HistoryQuestionKey("s", "scan2"),), "cursor2", True
            )

        def maintain_question(self, sid, qid, **kwargs):
            calls.append(("maintain", qid))
            if qid == "hot":
                loop, owner = owner_slot[0]
                loop.call_soon_threadsafe(owner.offer_entries, "s", (str(len(calls)),))
            return "published"

    monkeypatch.setattr(maintenance, "HistoryProjectionRepository", Store)
    monkeypatch.setattr(maintenance, "HISTORY_COALESCE_SECONDS", 0)

    async def run():
        owner = maintenance.HistoryProjectionMaintainer(object())
        owner_slot.append((asyncio.get_running_loop(), owner))
        try:
            owner.offer_entries("s", ("hot",))
            await eventually(lambda: ("maintain", "scan2") in calls)
            assert ("maintain", "hot") in calls
            assert calls.index(("maintain", "scan1")) < calls.index(("page", "cursor1"))
        finally:
            await owner.aclose(deadline_monotonic=maintenance.monotonic() + 3)

    asyncio.run(run())


def test_periodic_reconcile_recovers_an_external_change_without_hint(monkeypatch):
    scans, maintained = [], []
    dirty = threading.Event()

    class Store:
        def __init__(self, provider):
            pass

        def scan_history_questions(self, after, width, **kwargs):
            scans.append(after)
            questions = (HistoryQuestionKey("s", "missed"),) if dirty.is_set() else ()
            return HistoryProjectionPage(questions, "last", True)

        def maintain_question(self, sid, qid, **kwargs):
            maintained.append(qid)
            dirty.clear()
            return "published"

    monkeypatch.setattr(maintenance, "HistoryProjectionRepository", Store)
    monkeypatch.setattr(maintenance, "HISTORY_RECONCILE_SECONDS", 0.01)

    async def run():
        owner = maintenance.HistoryProjectionMaintainer(object())
        try:
            await eventually(lambda: len(scans) == 1)
            dirty.set()
            await eventually(lambda: maintained == ["missed"])
            assert len(scans) >= 2
        finally:
            await owner.aclose(deadline_monotonic=maintenance.monotonic() + 3)

    asyncio.run(run())


def test_close_joins_real_thread_despite_repeated_cancellation_and_drops_backlog(
    monkeypatch,
):
    entered, release, physical_done = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    calls = []

    class Store:
        def __init__(self, provider):
            pass

        def scan_history_questions(self, after, width, **kwargs):
            return HistoryProjectionPage(
                (HistoryQuestionKey("s", "active"), HistoryQuestionKey("s", "backlog")),
                "last",
                True,
            )

        def maintain_question(self, sid, qid, **kwargs):
            calls.append(qid)
            entered.set()
            assert release.wait(3)
            physical_done.set()
            return "published"

    monkeypatch.setattr(maintenance, "HistoryProjectionRepository", Store)

    async def run():
        owner = maintenance.HistoryProjectionMaintainer(object())
        assert await asyncio.to_thread(entered.wait, 3)
        close = asyncio.create_task(
            owner.aclose(deadline_monotonic=maintenance.monotonic() + 3)
        )
        await asyncio.sleep(0)
        close.cancel()
        await asyncio.sleep(0)
        close.cancel()
        await asyncio.sleep(0)
        assert not close.done() and not physical_done.is_set()
        owner.offer_entries("s", ("late",))
        owner.offer_session("late")
        release.set()
        await close
        assert physical_done.is_set() and calls == ["active"]
        assert not owner._hints and not owner._pending and owner._task.done()

    asyncio.run(run())


def test_core_offers_entry_hints_without_a_live_host_route():
    core = KernelHostCore(model_runtime=test_model_runtime())
    offered = []
    core._history_maintenance = SimpleNamespace(
        offer_entries=lambda sid, ids: offered.append((sid, ids))
    )
    event = SimpleNamespace(
        session_id="s", subject=SimpleNamespace(slot=SubjectSlot.ENTRY, subject_id="e")
    )
    core._deliver_committed_events((event,))
    assert offered == [("s", ("e",))]


def test_continuous_hints_do_not_slide_first_due_forever(monkeypatch):
    entered = threading.Event()

    class Store:
        def __init__(self, provider):
            pass

        def scan_history_questions(self, after, width, **kwargs):
            return HistoryProjectionPage((), after, True)

        def resolve_history_questions(self, sid, ids, **kwargs):
            return (HistoryQuestionKey(sid, "q"),)

        def maintain_question(self, sid, qid, **kwargs):
            entered.set()
            return "published"

    monkeypatch.setattr(maintenance, "HistoryProjectionRepository", Store)
    monkeypatch.setattr(maintenance, "HISTORY_COALESCE_SECONDS", 0.02)

    async def run():
        owner = maintenance.HistoryProjectionMaintainer(object())
        started_during_append = False
        try:
            for i in range(30):
                owner.offer_entries("s", (str(i),))
                await asyncio.sleep(0.005)
                started_during_append |= entered.is_set()
            assert started_during_append
        finally:
            await owner.aclose(deadline_monotonic=maintenance.monotonic() + 3)

    asyncio.run(run())


def test_zero_document_question_is_evaluated_once_per_sweep(monkeypatch):
    calls = []

    class Store:
        def __init__(self, provider):
            pass

        def scan_history_questions(self, after, width, **kwargs):
            return HistoryProjectionPage(
                (HistoryQuestionKey("s", "zero"),), "last", True
            )

        def maintain_question(self, sid, qid, **kwargs):
            calls.append(qid)
            return "published"

    monkeypatch.setattr(maintenance, "HistoryProjectionRepository", Store)

    async def run():
        owner = maintenance.HistoryProjectionMaintainer(object())
        try:
            await eventually(lambda: calls == ["zero"])
            await asyncio.sleep(0.02)
            assert calls == ["zero"]
            owner.offer_session("fork")
            await eventually(lambda: len(calls) == 2)
            await asyncio.sleep(0.02)
            assert calls == ["zero", "zero"]
        finally:
            await owner.aclose(deadline_monotonic=maintenance.monotonic() + 3)

    asyncio.run(run())


def test_committed_fork_hint_precedes_propagating_caller_cancellation(
    monkeypatch, tmp_path: Path
):
    entered, release = threading.Event(), threading.Event()
    hints = []

    def fork(**kwargs):
        entered.set()
        assert release.wait(3)
        return SimpleNamespace(created=True)

    async def run():
        core = KernelHostCore(model_runtime=test_model_runtime())
        core._repository = SimpleNamespace(fork_conversation=fork)
        core._history_maintenance = SimpleNamespace(offer_session=hints.append)

        async def summary(*args, **kwargs):
            return SimpleNamespace(workspace_root=str(tmp_path))

        monkeypatch.setattr(core, "read_resumable_session", summary)
        task = asyncio.create_task(
            core.fork_conversation(
                source_session_id="source",
                anchor_entry_id="anchor",
                child_session_id="child",
                memory_domain_id="u_local",
            )
        )
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert hints == ["child"]
        assert not core._open_attempts

    asyncio.run(run())


@pytest.mark.postgres
def test_real_core_maintains_without_host_and_reconciles_fork_and_restart(
    isolated_migrated_postgres_database, tmp_path
):
    from time import monotonic
    import psycopg
    from pulsara_agent.identity import random_id
    from tests.test_conversation_fork import turn

    db = isolated_migrated_postgres_database
    runtime = test_model_runtime(postgres_dsn=db.runtime_dsn)

    def coverage(session_id):
        with psycopg.connect(db.runtime_dsn, autocommit=True) as c:
            return c.execute(
                "SELECT count(*),max(covered_entry_sequence) FROM pulsara_v3.history_search_documents WHERE session_id=%s",
                (session_id,),
            ).fetchone()

    async def wait_coverage(session_id):
        async with asyncio.timeout(5):
            while (await asyncio.to_thread(coverage, session_id))[0] == 0:
                await asyncio.sleep(0.005)

    async def run():
        core = KernelHostCore(model_runtime=runtime)
        repo = await core._ensure_resources()
        guard = await asyncio.to_thread(
            lambda: (
                repo.acquire_host_writer(
                    intent="NEW",
                    session_id=random_id("session"),
                    workspace_id=random_id("workspace"),
                    workspace_root=str(tmp_path),
                    writer_owner_id=random_id("host"),
                    lease_seconds=300,
                    deadline_monotonic=monotonic() + 30,
                ).guard
            )
        )
        try:
            _, _, anchor = await asyncio.to_thread(
                turn,
                repo,
                guard,
                "core history question",
                answer="source recall body",
            )
            assert not core._extension_routes
            await wait_coverage(guard.session_id)
            child = random_id("session")
            creation = await core.fork_conversation(
                source_session_id=guard.session_id,
                anchor_entry_id=anchor,
                child_session_id=child,
                memory_domain_id="u_local",
            )
            assert creation.created
            await wait_coverage(child)
            with psycopg.connect(db.runtime_dsn, autocommit=True) as c:
                assert (
                    c.execute(
                        "SELECT latest_event_sequence FROM pulsara_v3.sessions WHERE id=%s",
                        (child,),
                    ).fetchone()[0]
                    == 0
                )
            # Simulate a lost hint / other-process projection deletion. Startup
            # must use the same production metadata reconciliation path.
            await core.shutdown()
            with psycopg.connect(db.runtime_dsn, autocommit=True) as c:
                c.execute(
                    "DELETE FROM pulsara_v3.history_search_documents WHERE session_id=%s",
                    (child,),
                )
            core = KernelHostCore(model_runtime=runtime)
            await core._ensure_resources()
            await wait_coverage(child)
        finally:
            await core.shutdown()
            assert core._history_maintenance is None and core._repository is None

    asyncio.run(run())
