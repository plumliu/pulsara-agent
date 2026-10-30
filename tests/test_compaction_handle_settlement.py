"""Compaction freezes retain resource ownership through physical I/O settlement."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from threading import Event

import pytest

from pulsara_agent.conversation_kernel.compaction.contracts import ResolvedCompactionPolicy
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent
from tests.test_compaction_request_start_integration import _Model, _rows, _seed_history
from tests.test_round4_plan_host import _open_test_session

pytestmark = pytest.mark.postgres


def test_empty_adoption_late_freeze_is_drained_after_repeated_stop(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        model = _Model()
        model.summary.release.set()
        model.child_release.set()
        core, root = await _open_test_session(tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        entered, release = Event(), Event()
        handles = []
        safe = root._runner._safe_point
        original = safe.freeze_compaction_input

        def held_freeze(**kwargs):
            handle = original(**kwargs)
            handles.append(handle)
            entered.set()
            assert release.wait(10)
            return handle

        running = None
        try:
            root._compaction.policy = ResolvedCompactionPolicy(automatic_enabled=False)
            _seed_history(root)
            usage = await root.read_context_usage()
            ratio = usage["input_tokens"] / usage["input_budget_tokens"]
            root._compaction.policy = replace(root._compaction.policy, automatic_enabled=True,
                auto_trigger_ratio=ratio * 0.95, post_compaction_target_ratio=ratio * 0.85, minimum_reclaim_tokens=1)
            monkeypatch.setattr(safe, "freeze_compaction_input", held_freeze)
            running = asyncio.create_task(root.run_turn(PromptContent.text("Continue the exact current request."),
                command_id=random_id("command")))
            assert await asyncio.to_thread(entered.wait, 10)
            assert len(handles) == 1 and not handles[0]._closed
            snapshots = _rows(root, "SELECT id FROM pulsara_v3.context_snapshots WHERE session_id=%s",
                (root.session_id,))
            assert len(snapshots) == 1
            worker = root._active_task
            assert worker is not None
            worker.cancel("first stop")
            await asyncio.sleep(0)
            worker.cancel("repeated stop")
            await asyncio.sleep(0)
            assert not handles[0]._closed and not worker.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await running
            assert handles[0]._closed and safe.is_idle()
            assert model.root_requests == []
            assert len(model.summary.calls) == 1
            assert not any(task.get_name() == "kernel-physical-io:prepare_handle"
                for task in root._io._active)
        finally:
            release.set()
            if running is not None:
                if not running.done():
                    running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            await core.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["repeated_stop", "late_deadline", "read_exception"])
def test_idle_projection_freeze_settles_its_actual_returned_handle(
    tmp_path, monkeypatch, stage2_migrated_postgres_database, failure,
):
    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        model = _Model()
        core, root = await _open_test_session(tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        entered, release = Event(), Event()
        handles = []
        safe = root._runner._safe_point
        original = safe.freeze_compaction_input

        def held_freeze(**kwargs):
            handle = original(**kwargs)
            handles.append(handle)
            entered.set()
            assert release.wait(10)
            return handle

        def failed_read(*args, **kwargs):
            raise OSError("injected frozen source read error")

        running = None
        try:
            root._compaction.policy = ResolvedCompactionPolicy(automatic_enabled=False,
                planning_attempt_seconds=0.1 if failure == "late_deadline" else 120)
            _seed_history(root)
            monkeypatch.setattr(safe, "freeze_compaction_input", held_freeze)
            if failure == "read_exception":
                monkeypatch.setattr(root._runner.compaction._input_reader,
                    "read_frozen_compaction_cut", failed_read)
            command = random_id("command")
            running = asyncio.create_task(root.compact_context(command_id=command))
            assert await asyncio.to_thread(entered.wait, 10)
            assert len(handles) == 1 and not handles[0]._closed
            worker = root._manual_compaction_command_attempts[command]
            if failure == "repeated_stop":
                worker.cancel("first stop")
                await asyncio.sleep(0)
                worker.cancel("repeated stop")
                await asyncio.sleep(0)
            elif failure == "late_deadline":
                await asyncio.sleep(0.15)
            assert not handles[0]._closed and not worker.done()
            release.set()
            result = await asyncio.wait_for(running, 10)
            assert result.disposition.value == "FAILED"
            assert handles[0]._closed and safe.is_idle()
            assert len(model.summary.calls) == 0 and model.root_requests == []
            assert _rows(root, "SELECT id FROM pulsara_v3.context_snapshots WHERE session_id=%s",
                (root.session_id,)) == []
            assert not any(task.get_name() == "kernel-physical-io:prepare_handle"
                for task in root._io._active)
        finally:
            release.set()
            if running is not None:
                if not running.done():
                    running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            await core.shutdown()
    asyncio.run(scenario())
