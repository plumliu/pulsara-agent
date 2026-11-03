"""Real successor publication keeps one stoppable settlement owner."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent
from tests.test_compaction_host_lifecycle import eventually, snapshot
from tests.test_round4_plan_host import _EnterPlanThenTextModel, _open_test_session

pytestmark = pytest.mark.postgres


def _terminal_turn(session, turn_id):
    return next(row for row in snapshot(session)[0] if row["id"] == turn_id)


def test_automatic_plan_handoff_stops_successor_before_cell_join(
    tmp_path: Path, monkeypatch, stage2_migrated_postgres_database,
):
    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        model = _EnterPlanThenTextModel()
        core, session = await _open_test_session(
            tmp_path=tmp_path, model=model, monkeypatch=monkeypatch,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
        )
        handoff_entered, handoff_release = asyncio.Event(), asyncio.Event()
        cancel_entered, cancel_release = asyncio.Event(), asyncio.Event()
        original_finish = session._finish_root_chain
        original_stop = session._code_mode.stop_turn
        original_mark = session._code_mode.mark_turn_stopped
        stop_fences = []

        def capture_stop_fence(turn_id):
            stop_fences.append((turn_id, session._active_task.cancelling(),
                                session._active_cancellation_intent.cause))
            original_mark(turn_id)

        async def held_handoff(result, cancellation_intent):
            if result.continuation_turn_id is not None:
                handoff_entered.set()
                await handoff_release.wait()
            return await original_finish(result, cancellation_intent)

        async def held_physical_cancel(turn_id):
            cancel_entered.set()
            await cancel_release.wait()
            return await original_stop(turn_id)

        monkeypatch.setattr(session, "_finish_root_chain", held_handoff)
        monkeypatch.setattr(session._code_mode, "stop_turn", held_physical_cancel)
        monkeypatch.setattr(session._code_mode, "mark_turn_stopped", capture_stop_fence)
        running = asyncio.create_task(session.run_turn(PromptContent.text("Enter Plan.")))
        try:
            await asyncio.wait_for(handoff_entered.wait(), 10)
            pending = session._pending_root_successor
            assert pending is not None
            successor_id = pending.successor_turn_id
            assert _terminal_turn(session, successor_id)["status"] == "RUNNING"
            outcome = await session.request_stop_turn(
                command_id=random_id("command"), target_turn_id=pending.origin_turn_id,
                admission_deadline_ms=session.control_admission_deadline_ms(),
                expected_session_id=session.session_id,
                expected_host_session_id=session.host_session_id,
            )
            assert outcome.status == "PENDING"
            await asyncio.wait_for(cancel_entered.wait(), 10)
            assert stop_fences[0][0] == pending.origin_turn_id
            assert stop_fences[0][1] == 0
            assert stop_fences[0][2].value == "USER_REQUEST"
            assert session._active_task.cancelling() > 0
            assert session._active_cancellation_intent.cause.value == "USER_REQUEST"
            handoff_release.set()

            async def settled():
                return _terminal_turn(session, successor_id)["status"] != "RUNNING"
            await eventually(settled)
            assert _terminal_turn(session, successor_id)["terminal_reason"] == "USER_STOPPED"
            assert len(model.requests) == 1
            assert session._pending_root_successor is None
            cancel_release.set()
            await asyncio.wait_for(session._user_control_attempts[outcome.command_id].task, 10)
            with pytest.raises(asyncio.CancelledError):
                await running
        finally:
            handoff_release.set()
            cancel_release.set()
            await asyncio.gather(running, return_exceptions=True)
            await core.shutdown()
    asyncio.run(scenario())
