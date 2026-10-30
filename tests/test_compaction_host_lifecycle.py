"""Host admission and stop remain independent of first provider preparation."""
from __future__ import annotations

import asyncio
from threading import Event
from time import monotonic

import pytest
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionScope, CompactionTrigger,
)
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent, text_part_values
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.test_round4_plan_host import _DetachedDraftModel, _open_test_session, _text

pytestmark = pytest.mark.postgres


class TextModel(_DetachedDraftModel):
    async def stream(self, request):
        self.requests.append(request)
        for event in _text("DONE"):
            yield event


async def eventually(predicate):
    async with asyncio.timeout(15):
        while not await predicate():
            await asyncio.sleep(0.01)


def snapshot(session):
    with session.repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        turns = connection.execute(
            "SELECT * FROM pulsara_v3.turns WHERE session_id=%s ORDER BY accepted_at,id",
            (session.session_id,),
        ).fetchall()
        queue = connection.execute(
            "SELECT * FROM pulsara_v3.prompt_queue_items WHERE session_id=%s ORDER BY queue_sequence",
            (session.session_id,),
        ).fetchall()
        entries = connection.execute(
            "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s ORDER BY entry_sequence",
            (session.session_id,),
        ).fetchall()
        return turns, queue, entries


@pytest.mark.parametrize("stop", [False, True])
def test_queue_and_redirect_remain_available_inside_first_preparation_fence(
    tmp_path, monkeypatch, stage2_migrated_postgres_database, stop,
):
    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        model = TextModel()
        core, session = await _open_test_session(
            tmp_path=tmp_path, model=model, monkeypatch=monkeypatch,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
        )
        entered, release = asyncio.Event(), asyncio.Event()
        prepare = session._runner._provider_dispatch.prepare
        first = True

        async def gated_prepare(**kwargs):
            nonlocal first
            if first:
                first = False
                async def paused():
                    entered.set()
                    await release.wait()
                try:
                    await session._compaction.run_fenced(
                        scope=CompactionScope(session.session_id, session.workspace.workspace_key,
                            kwargs["turn_id"], session._session_scope_kind),
                        trigger=CompactionTrigger.AUTO_ACTIVE_CONTEXT,
                        attempt_id=random_id("attempt"), operation=paused,
                    )
                except BaseException:
                    if kwargs.get("existing_handle") is not None:
                        kwargs["existing_handle"].close()
                    raise
            return await prepare(**kwargs)

        monkeypatch.setattr(session._runner._provider_dispatch, "prepare", gated_prepare)
        try:
            command = random_id("command")
            await session.submit_prompt(command_id=command, content=PromptContent.text("ORIGINAL_R1"))
            await asyncio.wait_for(entered.wait(), 10)
            owner = session._active_task
            running = await session.query_command(command)
            assert running.prompt_delivery.queue_status == "CONSUMED"
            assert running.prompt_delivery.execution.status == "running"
            assert session.active_root_turn_id() == running.target_id
            assert not model.requests
            turns, _, entries = snapshot(session)
            assert [turn["status"] for turn in turns] == ["RUNNING"]
            assert len(entries) == 1
            q1 = await session.submit_prompt(command_id=random_id("command"), content=PromptContent.text("NEXT_Q1"))
            q2 = await session.submit_prompt(command_id=random_id("command"), content=PromptContent.text("STEER_Q2"))
            steer = await session.steer_queued_prompt(
                command_id=random_id("command"), source_queue_item_id=q2.prompt_delivery.queue_item_id,
                target_turn_id=running.target_id,
            )
            assert steer.public_code == "PROMPT_STEER_QUEUED"
            assert session._active_task is owner
            assert not session._compaction_write_reservations
            if stop:
                await session.request_stop_turn(
                    command_id=random_id("command"), target_turn_id=running.target_id,
                    admission_deadline_ms=session.control_admission_deadline_ms(),
                    expected_session_id=session.session_id,
                    expected_host_session_id=session.host_session_id,
                )
                await asyncio.gather(owner, return_exceptions=True)
            else:
                release.set()
                await asyncio.shield(owner)
            async def finished():
                result = await session.query_command(q1.command_id)
                return result.prompt_delivery.execution.status == "completed"
            await eventually(finished)
            original = await session.query_command(command)
            redirected = await session.query_command(steer.command_id)
            assert original.prompt_delivery.execution.status == ("stopped" if stop else "completed")
            assert redirected.prompt_delivery.queue_status == ("REJECTED" if stop else "CONSUMED")
            assert len(model.requests) == (1 if stop else 2)
            first_input = str(text_part_values(part for msg in model.requests[0].compiled_input.messages for part in msg.content))
            assert ("STEER_Q2" in first_input) is not stop
            _, _, entries = snapshot(session)
            original_entries = [entry for entry in entries if entry["turn_id"] == running.target_id]
            assert len([entry for entry in original_entries if entry["entry_kind"] == "USER_MESSAGE"]) == 1
            assert len([entry for entry in original_entries if entry["entry_kind"] == "USER_STEER"]) == (0 if stop else 1)
        finally:
            release.set()
            await core.shutdown()
    asyncio.run(scenario())


def test_start_ack_unknown_repeated_stop_drains_original_owner_without_provider_open(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        model = TextModel()
        core, session = await _open_test_session(
            tmp_path=tmp_path, model=model, monkeypatch=monkeypatch,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
        )
        committed, release = Event(), Event()
        consume = session.repository.consume_prepared_prompt_head
        calls = 0
        def lost_ack(*args, **kwargs):
            nonlocal calls
            calls += 1
            consume(*args, **kwargs)
            committed.set()
            assert release.wait(15)
            raise OSError("simulated COMMIT acknowledgement loss")
        monkeypatch.setattr(session.repository, "consume_prepared_prompt_head", lost_ack)
        try:
            command = random_id("command")
            await session.submit_prompt(command_id=command, content=PromptContent.text("STOP_BEFORE_FIRST_PREPARATION"))
            assert await asyncio.to_thread(committed.wait, 10)
            owner = session._active_task
            turn_id = snapshot(session)[0][0]["id"]
            assert owner is not None
            for _ in range(2):
                await session.request_stop_turn(
                    command_id=random_id("command"), target_turn_id=turn_id,
                    admission_deadline_ms=session.control_admission_deadline_ms(),
                    expected_session_id=session.session_id,
                    expected_host_session_id=session.host_session_id,
                )
            await asyncio.sleep(0)
            assert not owner.done()
            release.set()
            await asyncio.wait_for(asyncio.gather(owner, return_exceptions=True), 10)
            turns, queue, entries = snapshot(session)
            assert calls == 1
            assert turns[0]["status"] == "INTERRUPTED"
            assert turns[0]["terminal_reason"] == "USER_STOPPED"
            assert queue[0]["status"] == "CONSUMED"
            assert len(entries) == 1
            assert not model.requests
            assert session.active_root_turn_id() is None
        finally:
            release.set()
            await core.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("stop", [False, True])
def test_idle_manual_operation_holds_execution_but_accepts_later_queue_and_retry(
    tmp_path, monkeypatch, stage2_migrated_postgres_database, stop,
):
    from pulsara_agent.conversation_kernel.compaction.contracts import CompactionDisposition
    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        model = TextModel()
        core, session = await _open_test_session(
            tmp_path=tmp_path, model=model, monkeypatch=monkeypatch,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
        )
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def failed_summary(**kwargs):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            raise RuntimeError("summary service failed before adoption")
        monkeypatch.setattr(session._runner, "compact_idle_turn", failed_summary)
        try:
            await session.run_turn(PromptContent.text("HISTORY_FOR_MANUAL"), command_id=random_id("command"))
            command = random_id("command")
            manual = asyncio.create_task(session.compact_context(command_id=command))
            await asyncio.wait_for(entered.wait(), 10)
            assert session.active_root_turn_id() is None
            assert (await session.query_command(command)).public_code == "COMPACTION_RUNNING"
            repeated = asyncio.create_task(session.compact_context(command_id=command))
            queued = await session.submit_prompt(command_id=random_id("command"), content=PromptContent.text("AFTER_MANUAL"))
            assert queued.prompt_delivery.queue_status == "PENDING"
            busy = await session.compact_context(command_id=random_id("command"))
            assert busy.disposition is CompactionDisposition.REJECTED_BUSY
            assert len(model.requests) == 1
            if stop:
                stop_commands = [random_id("command"), random_id("command")]
                for stop_command in stop_commands:
                    deadline = session.control_admission_deadline_ms()
                    pending = await session.request_stop_context_compaction(
                        command_id=stop_command, target_command_id=command,
                        admission_deadline_ms=deadline,
                        expected_session_id=session.session_id,
                        expected_host_session_id=session.host_session_id,
                    )
                    assert pending.user_control.target.target_id == command
                    repeated_stop = await session.request_stop_context_compaction(
                        command_id=stop_command, target_command_id=command,
                        admission_deadline_ms=deadline,
                        expected_session_id=session.session_id,
                        expected_host_session_id=session.host_session_id,
                    )
                    assert repeated_stop == pending
                await asyncio.gather(*(session._user_control_attempts[x].task for x in stop_commands))
                assert all(session._user_control_attempts[x].outcome.public_code == "COMPACTION_STOPPED" for x in stop_commands)
            else:
                release.set()
            first, second = await asyncio.gather(manual, repeated)
            assert first == second
            assert first.disposition is CompactionDisposition.FAILED
            assert calls == 1
            async def finished():
                return (await session.query_command(queued.command_id)).prompt_delivery.execution.status == "completed"
            await eventually(finished)
            assert len(model.requests) == 2
            assert (await session.compact_context(command_id=command)) == first
            assert (await session.query_command(command)).public_code == "FAILED"
            assert len(snapshot(session)[0]) == 2
            if stop:
                entered.clear()
                release.clear()
                next_command = random_id("command")
                next_manual = asyncio.create_task(session.compact_context(command_id=next_command))
                await asyncio.wait_for(entered.wait(), 10)
                stale = await session.request_stop_context_compaction(
                    command_id=random_id("command"), target_command_id=command,
                    admission_deadline_ms=session.control_admission_deadline_ms(),
                    expected_session_id=session.session_id,
                    expected_host_session_id=session.host_session_id,
                )
                assert stale.public_code == "CONTROL_TARGET_UNAVAILABLE"
                assert not next_manual.done()
                assert session.active_manual_compaction_command_id() == next_command
                await session.stop_manual_compaction(command_id=next_command)
                assert (await next_manual).public_code == "COMPACTION_CANCELLED"
        finally:
            release.set()
            await core.shutdown()
    asyncio.run(scenario())


def test_queue_commit_wins_before_manual_command_acceptance(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    from pulsara_agent.conversation_kernel.compaction.contracts import CompactionDisposition
    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        model = TextModel()
        core, session = await _open_test_session(
            tmp_path=tmp_path, model=model, monkeypatch=monkeypatch,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
        )
        entered, release = Event(), Event()
        accept = session.repository.accept_manual_compaction_command
        def paused_accept(*args, **kwargs):
            entered.set()
            assert release.wait(15)
            return accept(*args, **kwargs)
        monkeypatch.setattr(session.repository, "accept_manual_compaction_command", paused_accept)
        try:
            await session.run_turn(PromptContent.text("HISTORY"), command_id=random_id("command"))
            command = random_id("command")
            manual = asyncio.create_task(session.compact_context(command_id=command))
            assert await asyncio.to_thread(entered.wait, 10)
            queued = await session.submit_prompt(command_id=random_id("command"), content=PromptContent.text("QUEUE_WON"))
            assert queued.prompt_delivery.queue_status == "PENDING"
            release.set()
            result = await asyncio.wait_for(manual, 10)
            assert result.disposition is CompactionDisposition.REJECTED_BUSY
            assert await session.query_command(command) is None
            async def finished():
                return (await session.query_command(queued.command_id)).prompt_delivery.execution.status == "completed"
            await eventually(finished)
            assert len(model.requests) == 2
        finally:
            release.set()
            await core.shutdown()
    asyncio.run(scenario())


def test_unknown_terminal_write_and_status_read_keep_original_execution_owner(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        model = TextModel()
        core, session = await _open_test_session(
            tmp_path=tmp_path, model=model, monkeypatch=monkeypatch,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
        )
        preparing, release_prepare = asyncio.Event(), asyncio.Event()
        retry_entered, release_retry, write_failed = Event(), Event(), Event()
        finalize = session._finalize_todo_run_activation
        interrupt = session.repository.interrupt_turn
        read_status = session.repository.read_turn_status
        writes, failed_reads = 0, 0
        async def paused_finalizer(*args, **kwargs):
            preparing.set()
            await release_prepare.wait()
            return await finalize(*args, **kwargs)
        def uncertain_write(*args, **kwargs):
            nonlocal writes
            writes += 1
            if writes == 1:
                write_failed.set()
                raise OSError("terminal acknowledgement is unknown")
            retry_entered.set()
            assert release_retry.wait(15)
            return interrupt(*args, **kwargs)
        def uncertain_read(*args, **kwargs):
            nonlocal failed_reads
            if write_failed.is_set() and failed_reads == 0:
                failed_reads += 1
                raise OSError("terminal confirmation temporarily unavailable")
            return read_status(*args, **kwargs)
        monkeypatch.setattr(session, "_finalize_todo_run_activation", paused_finalizer)
        monkeypatch.setattr(session.repository, "interrupt_turn", uncertain_write)
        monkeypatch.setattr(session.repository, "read_turn_status", uncertain_read)
        try:
            command = random_id("command")
            await session.submit_prompt(command_id=command, content=PromptContent.text("STOP_WITH_UNKNOWN_TERMINAL"))
            await asyncio.wait_for(preparing.wait(), 10)
            owner = session._active_task
            target = session.active_root_turn_id()
            for _ in range(2):
                await session.request_stop_turn(
                    command_id=random_id("command"), target_turn_id=target,
                    admission_deadline_ms=session.control_admission_deadline_ms(),
                    expected_session_id=session.session_id,
                    expected_host_session_id=session.host_session_id,
                )
            release_prepare.set()
            assert await asyncio.to_thread(retry_entered.wait, 10)
            assert session._active_task is owner
            assert session.active_root_turn_id() == target
            assert not owner.done()
            assert failed_reads == 1
            assert snapshot(session)[0][0]["status"] == "RUNNING"
            assert not model.requests
            release_retry.set()
            await asyncio.wait_for(asyncio.gather(owner, return_exceptions=True), 10)
            assert snapshot(session)[0][0]["terminal_reason"] == "USER_STOPPED"
            assert session.active_root_turn_id() is None
        finally:
            release_retry.set()
            release_prepare.set()
            await core.shutdown()
    asyncio.run(scenario())


def test_subagent_capacity_and_exact_message_target_survive_first_preparation_stop(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    from types import SimpleNamespace
    from pulsara_agent.conversation_kernel.runner import ConversationKernelRunner
    from pulsara_agent.primitives.permission import PermissionMode
    from tests.test_agent_session_host import AgentModel
    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        model = AgentModel()
        model.first_child_release.set()
        core, root = await _open_test_session(
            tmp_path=tmp_path, model=model, monkeypatch=monkeypatch,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
        )
        model.root_id = root.session_id
        await core._agent_runtime.set_capacity_target(1)
        entered, release = asyncio.Event(), asyncio.Event()
        run = ConversationKernelRunner.run_accepted_turn
        first = True
        async def first_preparation(runner, turn_id, **kwargs):
            nonlocal first
            if runner._root_session_id is not None and first:
                first = False
                entered.set()
                await release.wait()
            return await run(runner, turn_id, **kwargs)
        monkeypatch.setattr(ConversationKernelRunner, "run_accepted_turn", first_preparation)
        released = []
        original_release = core._agent_runtime.release
        async def physical_release(session_id):
            if session_id in core._agent_runtime._occupied:
                released.append(session_id)
            await original_release(session_id)
        monkeypatch.setattr(core._agent_runtime, "release", physical_release)
        try:
            await root.run_turn(PromptContent.text("Start W1"), command_id=random_id("command"),
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            await asyncio.wait_for(entered.wait(), 10)
            agents = await root._io.run(root.repository.list_root_agent_session_ids,
                root_session_id=root.session_id, deadline_monotonic=monotonic()+10)
            model.agent_id = agents[0]
            child = core._live_agent_session(model.agent_id)
            w1_turn = child.active_root_turn_id()
            assert w1_turn is not None
            works = await root._io.run(root.repository.list_agent_works,
                root_session_id=root.session_id, agent_id=model.agent_id, limit=10,
                deadline_monotonic=monotonic()+10)
            w1 = works[0]["work_id"]
            assert works[0]["status"] == "running"
            sent = await core._send_agent_message(model.agent_id, "W1_ONLY_MESSAGE",
                SimpleNamespace(session_id=root.session_id, work_id=None))
            assert sent["delivery"] == "queued"
            assert sent["work"]["work_id"] == w1
            await root.run_turn(PromptContent.text("Queue W2"), command_id=random_id("command"),
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            works = await root._io.run(root.repository.list_agent_works,
                root_session_id=root.session_id, agent_id=model.agent_id, limit=10,
                deadline_monotonic=monotonic()+10)
            assert {work["status"] for work in works} == {"running", "queued"}
            assert await core._agent_runtime.capacity_state() == (1, 1)
            await core._stop_agent_active_work(model.agent_id, w1)
            async def w2_completed():
                current = await root._io.run(root.repository.list_agent_works,
                    root_session_id=root.session_id, agent_id=model.agent_id, limit=10,
                    deadline_monotonic=monotonic()+10)
                return len(current) == 2 and {work["status"] for work in current} == {"stopped", "completed"}
            await eventually(w2_completed)
            async def idle():
                return child.active_root_turn_id() is None
            await eventually(idle)
            assert released == [model.agent_id, model.agent_id]
            assert await core._agent_runtime.capacity_state() == (1, 0)
            _, queued, _ = snapshot(child)
            message = next(row for row in queued if row["input_origin"] == "AGENT_MESSAGE")
            assert message["target_turn_id"] == w1_turn
            assert message["status"] == "REJECTED"
            assert len(model.child_requests) == 1
            assert model.child_requests[0].turn_id != w1_turn
            contents = "\n".join(text_part_values(part for msg in model.child_requests[0].compiled_input.messages for part in msg.content))
            assert "W1_ONLY_MESSAGE" not in contents
        finally:
            release.set()
            await core.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("confirmation_error", [False, True])
def test_determined_start_rejection_is_terminal_without_scheduler_resubmission(
    tmp_path, monkeypatch, stage2_migrated_postgres_database, confirmation_error,
):
    from pulsara_agent.conversation_kernel.repository import ConversationKernelConflict
    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        model = TextModel()
        core, session = await _open_test_session(
            tmp_path=tmp_path, model=model, monkeypatch=monkeypatch,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
        )
        consume = session.repository.consume_prepared_prompt_head
        calls = 0
        def rejected(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise ConversationKernelConflict("deterministic request start refusal")
        monkeypatch.setattr(session.repository, "consume_prepared_prompt_head", rejected)
        confirm = session.repository.confirm_prepared_prompt_head_consumption
        if confirmation_error:
            def refused_confirmation(**_kwargs):
                raise ValueError("deterministic confirmation contract failure")
            monkeypatch.setattr(session.repository, "confirm_prepared_prompt_head_consumption", refused_confirmation)
        try:
            command = random_id("command")
            await session.submit_prompt(command_id=command, content=PromptContent.text("REFUSED_START"))
            async def rejected_request():
                outcome = await session.query_command(command)
                return outcome.prompt_delivery.queue_status == "REJECTED"
            await eventually(rejected_request)
            outcome = await session.query_command(command)
            assert outcome.public_code == "REQUEST_START_REJECTED"
            assert calls == 1
            assert not snapshot(session)[0]
            assert not model.requests
            monkeypatch.setattr(session.repository, "consume_prepared_prompt_head", consume)
            monkeypatch.setattr(session.repository, "confirm_prepared_prompt_head_consumption", confirm)
            next_command = random_id("command")
            await session.submit_prompt(command_id=next_command, content=PromptContent.text("NEXT_VALID_REQUEST"))
            async def finished():
                return (await session.query_command(next_command)).prompt_delivery.execution.status == "completed"
            await eventually(finished)
            assert calls == 1
            assert len(model.requests) == 1
        finally:
            await core.shutdown()
    asyncio.run(scenario())


def test_manual_is_busy_during_user_prompt_hook_preparation_and_never_revives(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    from pulsara_agent.conversation_kernel.compaction.contracts import CompactionDisposition
    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        model = TextModel()
        core, session = await _open_test_session(
            tmp_path=tmp_path, model=model, monkeypatch=monkeypatch,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
        )
        entered, release = asyncio.Event(), asyncio.Event()
        dispatch = session._dispatch_user_prompt_hook
        async def paused_hook(**kwargs):
            entered.set()
            await release.wait()
            return await dispatch(**kwargs)
        try:
            await session.run_turn(PromptContent.text("HISTORY"), command_id=random_id("command"))
            monkeypatch.setattr(session, "_dispatch_user_prompt_hook", paused_hook)
            command = random_id("command")
            submit = asyncio.create_task(session.submit_prompt(command_id=command, content=PromptContent.text("HOOK_PENDING")))
            await asyncio.wait_for(entered.wait(), 10)
            manual_command = random_id("command")
            busy = await session.compact_context(command_id=manual_command)
            assert busy.disposition is CompactionDisposition.REJECTED_BUSY
            assert not session.active_manual_compaction_command_id()
            assert len(snapshot(session)[0]) == 1
            release.set()
            await submit
            async def finished():
                return (await session.query_command(command)).prompt_delivery.execution.status == "completed"
            await eventually(finished)
            assert await session.query_command(manual_command) is None
            assert len(model.requests) == 2
            assert not session._manual_compaction_command_attempts
        finally:
            release.set()
            await core.shutdown()
    asyncio.run(scenario())


def test_manual_stop_settles_control_when_owner_is_cancelled_before_first_run(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        model = TextModel()
        core, session = await _open_test_session(
            tmp_path=tmp_path, model=model, monkeypatch=monkeypatch,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
        )
        entered = False
        async def not_started():
            nonlocal entered
            entered = True
            raise AssertionError("cancelled operation must never begin")
        try:
            command = random_id("command")
            task = session._compaction.start_owned(not_started(), name="kernel-manual-before-first-run")
            session._manual_compaction_command_attempts[command] = task
            task.add_done_callback(lambda _task: session._manual_compaction_command_attempts.pop(command, None))
            task.cancel()
            stop_command = random_id("command")
            pending = await session.request_stop_context_compaction(
                command_id=stop_command, target_command_id=command,
                admission_deadline_ms=session.control_admission_deadline_ms(),
                expected_session_id=session.session_id,
                expected_host_session_id=session.host_session_id,
            )
            assert pending.status == "PENDING"
            await asyncio.wait_for(session._user_control_attempts[stop_command].task, 10)
            outcome = session._user_control_attempts[stop_command].outcome
            assert outcome.status == "SUCCEEDED"
            assert outcome.public_code == "COMPACTION_STOPPED"
            assert outcome.user_control.execution.value == "FINISHED"
            assert not entered
            assert session.active_manual_compaction_command_id() is None
            assert not session._compaction.has_active_work()
            assert not snapshot(session)[0]
        finally:
            await core.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["confirm_manual_compaction_command", "validate_host_writer"])
def test_determined_manual_confirmation_failure_does_not_retry_or_leave_busy(
    tmp_path, monkeypatch, stage2_migrated_postgres_database, operation,
):
    from pulsara_agent.conversation_kernel.compaction.contracts import CompactionDisposition
    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        model = TextModel()
        core, session = await _open_test_session(
            tmp_path=tmp_path, model=model, monkeypatch=monkeypatch,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
        )
        original = getattr(session.repository, operation)
        calls = 0
        def refused(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise ValueError("deterministic manual confirmation contract failure")
        try:
            await session.run_turn(PromptContent.text("HISTORY"), command_id=random_id("command"))
            monkeypatch.setattr(session.repository, operation, refused)
            command = random_id("command")
            result = await asyncio.wait_for(session.compact_context(command_id=command), 10)
            assert result.disposition is CompactionDisposition.FAILED
            assert calls == 1
            assert session.active_manual_compaction_command_id() is None
            assert await session.query_command(command) is None
            monkeypatch.setattr(session.repository, operation, original)
            await session.run_turn(PromptContent.text("CONTINUE_AFTER_REFUSED_MANUAL"), command_id=random_id("command"))
            assert len(model.requests) == 2
        finally:
            monkeypatch.setattr(session.repository, operation, original)
            await core.shutdown()
    asyncio.run(scenario())


def test_human_steer_during_ordinary_summary_after_question_resolution(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    from pulsara_agent.conversation_kernel.compaction.contracts import ResolvedCompactionPolicy
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.conversation_kernel.repository import PlanQuestionAnswer
    from pulsara_agent.primitives.plan_workflow import PlanQuestionAnswerKind
    from tests.test_compaction_request_start_integration import _SUMMARY, _SummaryTransport
    from tests.test_stage2_conversation_runner import _CompactionScriptedModel
    from tests.test_round4_plan_host import _PreparedTestExecution, _tool_call
    class QuestionSummaryModel(_CompactionScriptedModel):
        def __init__(self):
            super().__init__([], "Prior investigation finished; follow the current user's exact guidance.")
            self.summary_transport = _SummaryTransport()
        def preflight_execution(self, request, *, append_candidate, install_authority):
            return _PreparedTestExecution(self, request, append_candidate, install_authority)
        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                events = _text("EARLIER_EVIDENCE " + "earlier investigation details completed successfully " * 6000)
            elif len(self.requests) == 2:
                events = _tool_call("ask_plan_question", random_id("call"), {
                    "question": "Choose the next investigation.", "options": [], "allow_free_text": True,
                })
            else:
                events = _text("GUIDANCE_DELIVERED")
            for event in events:
                yield event
    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        model = QuestionSummaryModel()
        core, session = await _open_test_session(
            tmp_path=tmp_path, model=model, monkeypatch=monkeypatch,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
        )
        running = first = second = None
        try:
            await session.run_turn(PromptContent.text("Collect prior evidence."), command_id=random_id("command"))
            await session.enter_plan(command_id=random_id("command"), entry_reason="ask before continuing",
                resume_permission_mode=PermissionMode.ACCEPT_EDITS)
            running = asyncio.create_task(session.run_turn(PromptContent.text("ASK_WITH_PLAN"),
                command_id=random_id("command"), requested_permission_mode=PermissionMode.ACCEPT_EDITS))
            async def opened():
                return await session._plan_interactions.current_open() is not None
            await eventually(opened)
            target = session.active_root_turn_id()
            session._compaction.policy = ResolvedCompactionPolicy(
                auto_trigger_ratio=0.20, post_compaction_target_ratio=0.15,
                minimum_reclaim_tokens=1,
            )
            q1 = await session.submit_prompt(command_id=random_id("command"), content=PromptContent.text("FIRST_HUMAN_GUIDANCE"))
            first = asyncio.create_task(session.steer_queued_prompt(command_id=random_id("command"),
                source_queue_item_id=q1.prompt_delivery.queue_item_id, target_turn_id=target))
            await first
            with session.repository.connection_provider.connection(
                lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
                deadline_monotonic=monotonic()+10,
            ) as connection:
                question = connection.execute("""SELECT i.id,i.status,i.control_tool_result_id,
                    i.plan_workflow_id,w.workflow_revision FROM pulsara_v3.plan_interactions i
                    JOIN pulsara_v3.plan_workflows w ON w.session_id=i.session_id AND w.id=i.plan_workflow_id
                    WHERE i.session_id=%s AND i.kind='QUESTION'""", (session.session_id,)).fetchone()
                assert question['status'] == 'OPEN' and question['control_tool_result_id'] is None
            assert not running.done() and len(model.requests) == 2
            assert not model.summary_transport.contexts
            assert [row['status'] for row in snapshot(session)[1] if row['input_origin'] == 'HUMAN_STEER'] == ['PENDING']
            await session.resolve_plan_question(command_id=random_id('command'),
                workflow_id=question['plan_workflow_id'],
                expected_workflow_revision=question['workflow_revision'], interaction_id=question['id'],
                answer=PlanQuestionAnswer(PlanQuestionAnswerKind.FREE_TEXT, free_text='Continue investigation'))
            await asyncio.wait_for(model.summary_transport.started.wait(), 15)
            q2 = await session.submit_prompt(command_id=random_id("command"), content=PromptContent.text("SECOND_HUMAN_GUIDANCE"))
            second = asyncio.create_task(session.steer_queued_prompt(command_id=random_id("command"),
                source_queue_item_id=q2.prompt_delivery.queue_item_id, target_turn_id=target))
            async def both_accepted():
                _, queue, _ = snapshot(session)
                return len([row for row in queue if row["delivery_mode"] == "STEER_ACTIVE_TURN" and row["status"] == "PENDING"]) == 2
            await eventually(both_accepted)
            # OPEN Question is a hard source boundary. This summary starts
            # only after the real answer and its tool result have settled.
            assert await session._plan_interactions.current_open() is None
            assert snapshot(session)[0][-1]["status"] == "RUNNING"
            assert len(model.summary_transport.contexts) == 1
            assert not model.summary_transport.release.is_set()
            model.summary_transport.release.set()
            await asyncio.wait_for(asyncio.gather(first, second, running), 15)
            assert len(model.requests) == 3
            final_input = "\n".join(text_part_values(part for message in model.requests[-1].compiled_input.messages for part in message.content))
            assert final_input.index("FIRST_HUMAN_GUIDANCE") < final_input.index("SECOND_HUMAN_GUIDANCE")
            assert "ASK_WITH_PLAN" in final_input
            assert "Continue investigation" in final_input
            assert _SUMMARY in final_input
            with session.repository.connection_provider.connection(
                lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
                deadline_monotonic=monotonic()+10,
            ) as connection:
                revisions = connection.execute("""SELECT revision_ordinal,context_snapshot_id
                    FROM pulsara_v3.turn_context_binding_revisions WHERE session_id=%s AND turn_id=%s
                    ORDER BY revision_ordinal""", (session.session_id, target)).fetchall()
                assert any(row['revision_ordinal'] > 0 and row['context_snapshot_id'] for row in revisions)
            turns, queue, entries = snapshot(session)
            current = next(row for row in turns if row["id"] == target)
            assert current["status"] == "COMPLETED"
            assert all(row["status"] == "CONSUMED" for row in queue if row["delivery_mode"] == "STEER_ACTIVE_TURN")
            assert len([row for row in entries if row["turn_id"] == target and row["entry_kind"] == "USER_STEER"]) == 2
            assert len(model.summary_transport.contexts) == 1
        finally:
            model.summary_transport.release.set()
            await core.shutdown()
            await asyncio.gather(*(task for task in (running, first, second) if task is not None), return_exceptions=True)
    asyncio.run(scenario())
