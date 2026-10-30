"""A failed terminal confirmation finishes the exact start-stop control."""
from __future__ import annotations

import asyncio
from threading import Event

import pytest

from pulsara_agent.identity import random_id
from tests.test_compaction_host_lifecycle import eventually
from tests.test_compaction_successor_control_races import _open_monitor, _terminal_turn

pytestmark = pytest.mark.postgres


def test_starting_turn_stop_confirmation_error_finishes_control_without_success(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    async def scenario():
        core, session, model, process_release = await _open_monitor(
            tmp_path, monkeypatch, stage2_migrated_postgres_database.runtime_dsn,
        )
        entered, release = Event(), Event()
        load = session.repository.read_started_turn_input_group
        terminal_read = session.repository.read_turn_terminal_outcome
        target = None
        def held_group(*args, **kwargs):
            nonlocal target
            target = kwargs["turn_id"]
            entered.set()
            assert release.wait(15)
            return load(*args, **kwargs)
        def uncertain_terminal(*args, **kwargs):
            if kwargs.get("turn_id") == target:
                raise OSError("the terminal control result is temporarily unreadable")
            return terminal_read(*args, **kwargs)
        monkeypatch.setattr(session.repository, "read_started_turn_input_group", held_group)
        try:
            process_release.touch()
            assert await asyncio.to_thread(entered.wait, 10)
            assert _terminal_turn(session, target)["status"] == "RUNNING"
            monkeypatch.setattr(session.repository, "read_turn_terminal_outcome", uncertain_terminal)
            command = random_id("command")
            pending = await session.request_stop_turn(
                command_id=command, target_turn_id=target,
                admission_deadline_ms=session.control_admission_deadline_ms(),
                expected_session_id=session.session_id,
                expected_host_session_id=session.host_session_id,
            )
            assert pending.status == "PENDING"
            control_task = session._user_control_attempts[command].task
            release.set()
            await asyncio.wait_for(asyncio.shield(control_task), 10)
            outcome = session._user_control_attempts[command].outcome
            assert outcome.status == "FAILED"
            assert outcome.public_code == "CONTROL_FAILED"
            assert outcome.user_control.execution.value == "FINISHED"
            assert outcome.user_control.root is None
            assert _terminal_turn(session, target)["terminal_reason"] == "USER_STOPPED"
            assert len(model.requests) == 3
            async def released():
                return not session._external_new_turn_accepting
            await eventually(released)
        finally:
            release.set()
            process_release.touch()
            monkeypatch.setattr(session.repository, "read_turn_terminal_outcome", terminal_read)
            await core.shutdown()
    asyncio.run(scenario())
