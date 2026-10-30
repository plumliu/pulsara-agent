"""Real successor publication keeps one stoppable settlement owner."""
from __future__ import annotations

import asyncio
from pathlib import Path
import shlex
import sys
from threading import Event
from time import monotonic

import pytest

from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent
from tests.test_compaction_host_lifecycle import eventually, snapshot
from tests.test_round2_terminal_host import _TerminalMonitorDogfoodModel, _text
from tests.test_round4_plan_host import _EnterPlanThenTextModel, _open_test_session

pytestmark = pytest.mark.postgres


class _MonitorThenTextModel(_TerminalMonitorDogfoodModel):
    async def _stream(self, request):
        if len(self.requests) <= 3:
            async for item in super()._stream(request):
                yield item
        else:
            for item in _text("text:after-monitor", "NEXT_REQUEST_COMPLETED"):
                yield item


async def _open_monitor(tmp_path, monkeypatch, postgres_dsn):
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PULSARA_TERMINAL_SHELL_SNAPSHOT", "0")
    release_path = tmp_path / "release-process"
    script = (
        "import time; from pathlib import Path; "
        "print('R2_INITIAL_SENTINEL', flush=True); "
        f"release = Path({str(release_path)!r});\n"
        "while not release.exists(): time.sleep(0.01)\n"
        "print('R2_COMPLETION_SENTINEL', flush=True)"
    )
    command = f"{shlex.quote(sys.executable)} -u -c {shlex.quote(script)}"
    model = _MonitorThenTextModel(command)
    core, session = await _open_test_session(
        tmp_path=tmp_path, model=model, monkeypatch=monkeypatch,
        postgres_dsn=postgres_dsn,
    )
    result = await session.run_turn(PromptContent.text("Run and monitor the process."))
    assert result.final_text == "MONITOR_REGISTERED_WAITING"
    return core, session, model, release_path


def _terminal_turn(session, turn_id):
    return next(row for row in snapshot(session)[0] if row["id"] == turn_id)


@pytest.mark.parametrize("stop,ack_reentry", [(False, False), (False, True), (True, False)],
    ids=["group-failure-releases-busy", "confirmed-full-group-failure", "stop-during-group-load"])
def test_terminal_full_group_load_preserves_exact_start_owner(
    tmp_path: Path, monkeypatch, stage2_migrated_postgres_database, stop, ack_reentry,
):
    async def scenario():
        core, session, model, process_release = await _open_monitor(
            tmp_path, monkeypatch, stage2_migrated_postgres_database.runtime_dsn,
        )
        entered, release = Event(), Event()
        original_load = session.repository.read_started_turn_input_group
        original_turn_id = None
        calls = 0

        def held_group(*args, **kwargs):
            nonlocal original_turn_id, calls
            calls += 1
            if calls == 1:
                original_turn_id = kwargs["turn_id"]
                entered.set()
                assert release.wait(15), "test did not release physical group read"
                if not stop:
                    raise RuntimeError("injected post-publication group read failure")
            return original_load(*args, **kwargs)

        monkeypatch.setattr(session.repository, "read_started_turn_input_group", held_group)
        start_writes = 0
        immediate_confirmations = 0
        if ack_reentry:
            original_accept = session.repository.accept_terminal_observation
            original_confirm = session.repository.confirm_terminal_observation_winner

            def lost_ack(*args, **kwargs):
                nonlocal start_writes
                start_writes += 1
                original_accept(*args, **kwargs)
                raise ConnectionError("injected committed observation ACK loss")

            def unavailable_immediate_confirmation(*args, **kwargs):
                nonlocal immediate_confirmations
                immediate_confirmations += 1
                if immediate_confirmations <= 2:
                    # Both immediate observers fail; the original immutable
                    # attempt is exact-confirmed by the next Host pass.
                    raise RuntimeError("injected unavailable immediate confirmation")
                return original_confirm(*args, **kwargs)

            monkeypatch.setattr(session.repository, "accept_terminal_observation", lost_ack)
            monkeypatch.setattr(session.repository, "confirm_terminal_observation_winner", unavailable_immediate_confirmation)
        control_task = None
        try:
            process_release.touch()
            assert await asyncio.to_thread(entered.wait, 10)
            assert original_turn_id is not None
            assert _terminal_turn(session, original_turn_id)["status"] == "RUNNING"
            assert len(model.requests) == 3
            if stop:
                outcome = await session.request_stop_turn(
                    command_id=random_id("command"), target_turn_id=original_turn_id,
                    admission_deadline_ms=session.control_admission_deadline_ms(),
                    expected_session_id=session.session_id,
                    expected_host_session_id=session.host_session_id,
                )
                assert outcome.status == "PENDING"
                control_task = session._user_control_attempts[outcome.command_id].task
                assert control_task is not None
            release.set()

            async def settled():
                return _terminal_turn(session, original_turn_id)["status"] == "INTERRUPTED"
            await eventually(settled)
            if control_task is not None:
                await asyncio.wait_for(asyncio.shield(control_task), 10)
            row = _terminal_turn(session, original_turn_id)
            assert row["terminal_reason"] == (
                "USER_STOPPED" if stop else "FOREGROUND_EXECUTION_INTERRUPTED"
            )
            assert len(model.requests) == 3

            async def reservation_released():
                return not session._external_new_turn_accepting
            await eventually(reservation_released)
            assert session._terminal_new_turn_observation_id is None
            assert session._external_new_turn_cancellation_intent is None
            _, _, entries = snapshot(session)
            assert len([row for row in entries if row["entry_kind"] == "TERMINAL_OBSERVATION"]) == 1
            events = session.repository.events_after(
                session_id=session.session_id, after_sequence=0, limit=64,
                deadline_monotonic=monotonic() + 5,
            )
            assert sum(event["event_type"] == "TerminalObservationAccepted" for event in events) == 1
            following = await session.run_turn(PromptContent.text("Continue after the settled monitor turn."))
            assert following.final_text == "NEXT_REQUEST_COMPLETED"
            assert len(model.requests) == 4
            assert calls == 1
            if ack_reentry:
                assert start_writes == 1
                assert immediate_confirmations >= 3
        finally:
            release.set()
            process_release.touch()
            await core.shutdown()
    asyncio.run(scenario())


def test_automatic_plan_handoff_preserves_stop_cause_before_physical_cancel(
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
            assert session._active_task.cancelling() == 0
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


@pytest.mark.parametrize("close", [False, True], ids=["repeated-raw-cancel", "host-close"])
@pytest.mark.parametrize("ack_reentry", [False, True], ids=["published-full", "confirmed-full"])
def test_terminal_full_physical_group_read_cancel_and_close_drain_original_owner(
    tmp_path: Path, monkeypatch, stage2_migrated_postgres_database, close, ack_reentry,
):
    async def scenario():
        core, session, model, process_release = await _open_monitor(
            tmp_path, monkeypatch, stage2_migrated_postgres_database.runtime_dsn,
        )
        entered, release = Event(), Event()
        original_load = session.repository.read_started_turn_input_group
        target_id = None
        load_calls = 0
        writes = 0
        confirms = 0
        if ack_reentry:
            accept = session.repository.accept_terminal_observation
            confirm = session.repository.confirm_terminal_observation_winner

            def lost_ack(*args, **kwargs):
                nonlocal writes
                writes += 1
                accept(*args, **kwargs)
                raise ConnectionError("injected committed observation ACK loss")

            def unavailable_immediate_confirmation(*args, **kwargs):
                nonlocal confirms
                confirms += 1
                if confirms <= 2:
                    raise RuntimeError("injected unavailable immediate confirmation")
                return confirm(*args, **kwargs)

            monkeypatch.setattr(session.repository, "accept_terminal_observation", lost_ack)
            monkeypatch.setattr(session.repository, "confirm_terminal_observation_winner", unavailable_immediate_confirmation)

        def held_group(*args, **kwargs):
            nonlocal target_id, load_calls
            load_calls += 1
            target_id = kwargs["turn_id"]
            entered.set()
            assert release.wait(15), "test did not release physical group read"
            return original_load(*args, **kwargs)

        monkeypatch.setattr(session.repository, "read_started_turn_input_group", held_group)
        close_task = None
        try:
            process_release.touch()
            assert await asyncio.to_thread(entered.wait, 10)
            assert target_id is not None
            assert _terminal_turn(session, target_id)["status"] == "RUNNING"
            owner = session._monitor_task
            original_intent = session._external_new_turn_cancellation_intent
            original_settlement = session._external_new_turn_settled
            assert original_intent is not None
            assert original_intent.turn_id == target_id
            assert not original_settlement.is_set()
            assert session._compaction_write_reservations
            assert len(model.requests) == 3
            if close:
                close_task = asyncio.create_task(core.close_session(session.host_session_id))

                async def close_started():
                    return session._closing
                await eventually(close_started)
                assert original_intent.cause.value == "HOST_SESSION_CLOSE"
                assert not close_task.done()
            else:
                for _ in range(3):
                    owner.cancel()
                    # Deliver each cancellation while the original physical
                    # read stays blocked, rather than collapsing cancel calls.
                    await asyncio.sleep(0)
            assert session._monitor_task is owner
            assert not owner.done()
            assert session._external_new_turn_cancellation_intent is original_intent
            assert session._external_new_turn_settled is original_settlement
            assert not original_settlement.is_set()
            assert _terminal_turn(session, target_id)["status"] == "RUNNING"
            assert len(model.requests) == 3
            release.set()
            if close_task is not None:
                await asyncio.wait_for(asyncio.shield(close_task), 15)
            else:
                result = await asyncio.wait_for(asyncio.gather(owner, return_exceptions=True), 15)
                assert isinstance(result[0], asyncio.CancelledError)
            row = _terminal_turn(session, target_id)
            assert row["status"] == "INTERRUPTED"
            assert row["terminal_reason"] == (
                "SESSION_CLOSED" if close else "FOREGROUND_EXECUTION_INTERRUPTED"
            )
            assert len(model.requests) == 3
            assert load_calls == 1
            assert original_settlement.is_set()
            assert not session._external_new_turn_accepting
            assert session._external_new_turn_cancellation_intent is None
            assert session._terminal_new_turn_observation_id is None
            assert not session._compaction_write_reservations
            _, _, entries = snapshot(session)
            assert len([row for row in entries if row["entry_kind"] == "TERMINAL_OBSERVATION"]) == 1
            events = session.repository.events_after(
                session_id=session.session_id, after_sequence=0, limit=64,
                deadline_monotonic=monotonic() + 5,
            )
            assert sum(event["event_type"] == "TerminalObservationAccepted" for event in events) == 1
            assert sum(event["event_type"] == "TurnInterrupted" and event["subject_turn_id"] == target_id for event in events) == 1
            if ack_reentry:
                assert writes == 1
                assert confirms >= 3
        finally:
            release.set()
            process_release.touch()
            if close_task is not None:
                await asyncio.wait_for(asyncio.shield(close_task), 15)
            await asyncio.wait_for(core.shutdown(), 15)
    asyncio.run(scenario())
