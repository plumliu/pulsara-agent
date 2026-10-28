"""Request state follows its own accepted queue, never the latest session turn."""
from __future__ import annotations

import asyncio
from time import monotonic

import pytest

from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.terminal_protocol.v3_gateway import _outcome_to_wire
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from tests.test_scheduled_tasks_host import setup, settled
from tests.test_stage2_kernel_host_dogfood import _SteerModelPort

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("stop_running", [False, True])
def test_request_lifecycle_and_independent_delivery(
    tmp_path, stage2_migrated_postgres_database, monkeypatch, stop_running
):
    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        model = _SteerModelPort()
        core, sessions, service, handle, _ = await setup(
            tmp_path, stage2_migrated_postgres_database, monkeypatch, model
        )
        session = handle.session
        try:
            command = random_id("command")
            accepted = await session.submit_prompt(
                command_id=command, content=PromptContent.text("First request")
            )
            delivery = accepted.prompt_delivery
            assert delivery.execution.to_dict() == {"status": "queued", "terminal_reason": None}
            await asyncio.wait_for(model.started.wait(), 5)
            running = await session.query_command(command)
            assert running.prompt_delivery.queue_item_id == delivery.queue_item_id
            assert running.prompt_delivery.execution.status == "running"
            assert running.status == "PENDING"
            assert _outcome_to_wire("running", running).prompt_delivery.execution.status == wire.REQUEST_EXECUTION_RUNNING

            cancelled_command = random_id("command")
            queued = await session.submit_prompt(
                command_id=cancelled_command, content=PromptContent.text("Cancel this queued request")
            )
            cancel = await session.cancel_queued_prompt(
                command_id=random_id("command"), source_queue_item_id=queued.prompt_delivery.queue_item_id
            )
            assert cancel.status == "SUCCEEDED"
            assert cancel.prompt_delivery.execution.status == "stopped"
            original = await session.query_command(cancelled_command)
            assert original.status == "REJECTED"
            assert original.prompt_delivery.execution.to_dict() == cancel.prompt_delivery.execution.to_dict()
            assert original.prompt_delivery.consumed_entry_id is None

            redirect_command = random_id("command")
            redirected = await session.submit_prompt(
                command_id=redirect_command, content=PromptContent.text("Additional direction")
            )
            steer_command = random_id("command")
            steer = await session.steer_queued_prompt(
                command_id=steer_command,
                source_queue_item_id=redirected.prompt_delivery.queue_item_id,
                target_turn_id=running.target_id,
            )
            assert steer.prompt_delivery.execution is None
            assert not _outcome_to_wire("steer", steer).prompt_delivery.HasField("execution")
            old = await session.query_command(redirect_command)
            assert old.prompt_delivery.execution.to_dict() == {
                "status": "stopped", "terminal_reason": "USER_REDIRECTED_TO_STEER"
            }
            assert old.prompt_delivery.queue_item_id != steer.prompt_delivery.queue_item_id

            if stop_running:
                stop = await session.request_stop_turn(
                    command_id=random_id("command"), admission_deadline_ms=session.control_admission_deadline_ms(),
                    expected_session_id=session.session_id,
                    expected_host_session_id=session.host_session_id,
                    target_turn_id=running.target_id,
                )
                assert stop.status == "PENDING"
            else:
                model.release.set()

            async def finished():
                outcome = await session.query_command(command)
                return outcome.prompt_delivery.execution.status in {"completed", "stopped"}
            await settled(finished)
            final = await session.query_command(command)
            execution = final.prompt_delivery.execution
            assert execution.status == ("stopped" if stop_running else "completed")
            assert execution.terminal_reason == ("USER_STOPPED" if stop_running else None)
            assert final.public_code == ("USER_STOPPED" if stop_running else "TURN_COMPLETED")
            exact = await asyncio.to_thread(
                session.repository.query_request_execution,
                session_id=session.session_id, queue_item_id=delivery.queue_item_id,
                deadline_monotonic=monotonic() + 5,
            )
            assert exact == execution
            assert final.prompt_delivery.queue_status == "CONSUMED"
            assert (await session.query_command(redirect_command)).prompt_delivery.execution == old.prompt_delivery.execution
            if not stop_running:
                delivered = await session.query_command(steer_command)
                assert delivered.status == "SUCCEEDED"
                assert delivered.public_code == "STEER_DELIVERED"
                assert delivered.prompt_delivery.execution is None
        finally:
            model.release.set()
            await service.aclose()
            await sessions.aclose()
            await core.shutdown()

    asyncio.run(scenario())


def test_scheduled_submission_confirmed_after_request_stops(
    tmp_path, stage2_migrated_postgres_database, monkeypatch
):
    from datetime import datetime, timezone
    from tests.test_scheduled_tasks_host import create

    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        model = _SteerModelPort()
        core, sessions, service, handle, _ = await setup(
            tmp_path, stage2_migrated_postgres_database, monkeypatch, model
        )
        session = handle.session
        try:
            await session.submit_prompt(command_id=random_id("command"), content=PromptContent.text("Running request"))
            await asyncio.wait_for(model.started.wait(), 5)
            task = await create(service, session)
            request = dict(
                client_command_id=random_id("command"), session_id=session.session_id,
                task_id=task["id"], expected_revision=task["revision"],
                request_at_utc=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            )
            submit = service._submit

            async def settled_submission(host, admission):
                accepted = await submit(host, admission)
                await host.cancel_queued_prompt(
                    command_id=random_id("command"), source_queue_item_id=accepted.prompt_delivery.queue_item_id
                )
                # The accepted request ended before submit confirmation reached its caller.
                confirmed = await host.query_command(admission.client_command_id)
                assert confirmed.status == "REJECTED"
                return confirmed

            monkeypatch.setattr(service, "_submit", settled_submission)
            result = await service.run_now(request)
            assert result["execution"]["status"] == "stopped"
            assert result["execution"]["terminal_reason"]
            assert not result["coalesced"]
            assert await service.run_now(request) == result
        finally:
            model.release.set()
            await service.aclose()
            await sessions.aclose()
            await core.shutdown()

    asyncio.run(scenario())
