"""Scheduler composes with the real Host/controller and ordinary ROOT queue."""

from __future__ import annotations
import asyncio
from datetime import datetime, timedelta, timezone
from time import monotonic
from uuid import uuid4
import shutil
import pytest
from psycopg.rows import dict_row
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.scheduling.service import ScheduledTaskService
from pulsara_agent.tool_permission import default_permission_policy
from pulsara_agent.web_app.session_controller import LocalSessionController
from pulsara_agent.web_app.session_controller import SessionControlRejected
from pulsara_agent.llm.model_connections import ModelCallBinding, ModelConnectionId
from pulsara_agent.workspace_identity import WorkspaceUnavailable
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.support.model_config import test_model_runtime, test_model_binding
from tests.test_stage2_kernel_host_dogfood import _SteerModelPort, _DogfoodModelPort

pytestmark = pytest.mark.postgres


async def settled(check):
    # Test observation timeout, not a production turn/task lifetime bound.
    async with asyncio.timeout(15):
        while not await check():
            await asyncio.sleep(0.01)


async def setup(tmp_path, db, monkeypatch, port):
    import pulsara_agent.conversation_kernel.host as host

    monkeypatch.setattr(host, "DirectKernelModelPort", lambda **_: port)
    monkeypatch.setattr(
        host.LocalMcpManagementService, "load_configs", lambda *a, **k: ()
    )
    runtime = test_model_runtime(postgres_dsn=db.runtime_dsn)
    root = tmp_path / "workspace"
    root.mkdir()
    core = KernelHostCore.production(model_runtime=runtime)
    sessions = LocalSessionController(
        core=core,
        permission_policy=default_permission_policy(),
        active_skill_names=frozenset(),
    )
    service = ScheduledTaskService(sessions)
    core.scheduled_tasks = service
    handle = await sessions.create_session(
        workspace_kind="project", workspace_path=str(root)
    )
    await handle.session.update_model_call_binding(test_model_binding(runtime))
    return core, sessions, service, handle, root


async def create(service, session, prompt="Scheduled report", once=False):
    schedule = {
        "contract": "scheduled-rule:v1",
        "kind": "interval",
        "anchor_at_utc": "2020-01-01T00:00:00+00:00",
        "seconds": 60,
    }
    if once:
        schedule = {
            "contract": "scheduled-rule:v1",
            "kind": "once",
            "run_at_utc": (datetime.now(timezone.utc) + timedelta(hours=1))
            .replace(microsecond=0)
            .isoformat(),
        }
    return await service.create(
        session_id=session.session_id,
        values=dict(
            name="Report",
            prompt=prompt,
            schedule=schedule,
            timezone="Asia/Shanghai",
            permission_mode="ask-permissions",
        ),
    )


def test_quick_session_initial_model_is_ready_for_a_scheduled_task(
    tmp_path, stage2_migrated_postgres_database, monkeypatch
):
    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        port = _SteerModelPort()
        core, sessions, service, _, _ = await setup(
            tmp_path, stage2_migrated_postgres_database, monkeypatch, port
        )
        try:
            binding = test_model_binding(core._model_runtime)
            with pytest.raises(SessionControlRejected) as failure:
                await sessions.create_session(
                    workspace_kind="quick",
                    model_call_binding=ModelCallBinding(
                        ModelConnectionId("model-connection:" + "f" * 32), None
                    ),
                )
            assert failure.value.public_code == "MODEL_BINDING_INVALID"
            assert not (tmp_path / "home" / "workspaces").exists()

            h = await sessions.create_session(
                workspace_kind="quick", model_call_binding=binding
            )
            assert await h.session.model_call_binding() == binding
            assert h.session.workspace.workspace_kind == "transient"
            assert h.session.workspace.workspace_root.is_dir()
            task = await service.create(
                session_id=h.session_id,
                values=dict(
                    name="Quick scheduled report",
                    prompt="Inspect progress",
                    schedule={
                        "contract": "scheduled-rule:v1", "kind": "daily",
                        "start_date": "2026-10-05", "time": "08:00",
                    },
                    timezone="Asia/Shanghai", permission_mode="bypass-permissions",
                ),
            )
            assert task["session_id"] == h.session_id
            assert task["timezone"] == "Asia/Shanghai"
            assert task["permission_mode"] == "bypass-permissions"
            assert not await queue(service, h.session_id)
            assert not port.requests
        finally:
            await service.aclose()
            await sessions.aclose()
            await core.shutdown()

    asyncio.run(scenario())


async def due(service, session, task):
    repo = await service._repository()

    # Integral UTC is the schedule protocol. Clock seam changes only the saved due.
    def write(*, deadline_monotonic):
        with repo._writer_transaction(
            session._lease.guard, deadline_monotonic=deadline_monotonic
        ) as c:
            c.execute(
                "UPDATE pulsara_v3.scheduled_tasks SET next_run_at=date_trunc('second',clock_timestamp())-interval '1 second' WHERE id=%s",
                (task["id"],),
            )

    await service._io.run(write, deadline_monotonic=monotonic() + 30)


async def queue(service, sid):
    def read(*, deadline_monotonic):
        repo = service.sessions.core._repository
        with repo.connection_provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as c:
            return c.execute(
                "SELECT * FROM pulsara_v3.prompt_queue_items WHERE session_id=%s ORDER BY queue_sequence",
                (sid,),
            ).fetchall()

    return await service._io.run(read, deadline_monotonic=monotonic() + 30)


def test_busy_fifo_coalescing_pause_and_source(
    tmp_path, stage2_migrated_postgres_database, monkeypatch
):
    async def scenario():
        port = _SteerModelPort()
        core, sessions, service, h, _ = await setup(
            tmp_path, stage2_migrated_postgres_database, monkeypatch, port
        )
        try:
            await h.session.submit_prompt(
                command_id="human:first", content=PromptContent.text("Human first")
            )
            await asyncio.wait_for(port.started.wait(), 15)
            task = await create(service, h.session)
            await due(service, h.session, task)
            await service.tick()
            await h.session.submit_prompt(
                command_id="human:second", content=PromptContent.text("Human second")
            )
            await due(service, h.session, task)
            await service.tick()
            rows = await queue(service, h.session_id)
            assert [r["input_origin"] for r in rows] == [
                "HUMAN_MESSAGE",
                "SCHEDULED_TASK",
                "HUMAN_MESSAGE",
            ]
            assert [r["status"] for r in rows] == ["CONSUMED", "PENDING", "PENDING"]
            assert len(port.requests) == 1
            paused = await service.mutate(
                task_id=task["id"], expected_revision=1, action="pause"
            )
            assert paused["status"] == "PAUSED"
            assert (await queue(service, h.session_id))[1]["status"] == "CANCELLED"
            port.release.set()

            async def done():
                return (await queue(service, h.session_id))[-1][
                    "status"
                ] == "CONSUMED" and h.session._active_task is None

            await settled(done)
            assert len(port.requests) == 2
            # Explicit run uses the same paused task, without changing its plan.
            req = dict(
                client_command_id=uuid4().hex,
                session_id=h.session_id,
                task_id=task["id"],
                expected_revision=paused["revision"],
                request_at_utc=datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat(),
            )
            accepted = await service.run_now(req)

            async def manualdone():
                return (await queue(service, h.session_id))[-1][
                    "status"
                ] == "CONSUMED" and h.session._active_task is None

            await settled(manualdone)
            assert (await service.get(task["id"]))["status"] == "PAUSED"
            from pulsara_agent.conversation_kernel.reader import (
                CanonicalProviderInputReader,
            )

            reader = CanonicalProviderInputReader(
                h.session.repository.connection_provider
            )
            snapshot = await service._io.run(
                reader.read_frozen_snapshot,
                port.requests[-1].cut,
                deadline_monotonic=monotonic() + 30,
            )
            last = snapshot.items[-1]
            assert (
                last.input_origin.value == "SCHEDULED_TASK"
                and last.scheduled_input.task_id == task["id"]
            )
            await service.mutate(
                task_id=task["id"],
                expected_revision=paused["revision"],
                action="delete",
            )
            confirmed = await service.run_now(req)
            assert (
                confirmed["queue_item_id"] == accepted["queue_item_id"]
                and confirmed["status"] == "CONSUMED"
            )
        finally:
            port.release.set()
            await service.aclose()
            await sessions.aclose()
            await core.shutdown()

    asyncio.run(scenario())


def test_missing_workspace_auto_pauses_without_mkdir_manual_keeps_plan(
    tmp_path, stage2_migrated_postgres_database, monkeypatch
):
    async def scenario():
        core, sessions, service, h, root = await setup(
            tmp_path,
            stage2_migrated_postgres_database,
            monkeypatch,
            _DogfoodModelPort(),
        )
        try:
            task = await create(service, h.session, once=True)
            await due(service, h.session, task)
            await sessions.aclose()
            # New management controller has no Host; cold task administration remains possible.
            sessions = LocalSessionController(
                core=core,
                permission_policy=default_permission_policy(),
                active_skill_names=frozenset(),
            )
            service.sessions = sessions
            shutil.rmtree(root)
            await service.tick()
            paused = await service.get(task["id"])
            assert paused["status"] == "PAUSED" and not root.exists()
            req = dict(
                client_command_id=uuid4().hex,
                session_id=h.session_id,
                task_id=task["id"],
                expected_revision=paused["revision"],
                request_at_utc=datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat(),
            )
            with pytest.raises(WorkspaceUnavailable):
                await service.run_now(req)
            assert (await service.get(task["id"]))["revision"] == paused["revision"]
            assert not root.exists() and await queue(service, h.session_id) == []
            await service.mutate(
                task_id=task["id"],
                expected_revision=paused["revision"],
                action="delete",
            )
        finally:
            await service.aclose()
            await sessions.aclose()
            await core.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("manual", [False, True])
def test_restart_wakes_completed_once_and_paused_manual_pending(
    tmp_path, stage2_migrated_postgres_database, monkeypatch, manual
):
    async def scenario():
        port = _SteerModelPort()
        core, sessions, service, h, _ = await setup(
            tmp_path, stage2_migrated_postgres_database, monkeypatch, port
        )
        runtime = core._model_runtime
        try:
            await h.session.submit_prompt(
                command_id="busy", content=PromptContent.text("Busy human")
            )
            await asyncio.wait_for(port.started.wait(), 15)
            task = await create(service, h.session, once=True)
            if manual:
                task = await service.mutate(
                    task_id=task["id"],
                    expected_revision=task["revision"],
                    action="pause",
                )
                await service.run_now(
                    dict(
                        client_command_id=uuid4().hex,
                        session_id=h.session_id,
                        task_id=task["id"],
                        expected_revision=task["revision"],
                        request_at_utc=datetime.now(timezone.utc)
                        .replace(microsecond=0)
                        .isoformat(),
                    )
                )
            else:
                await due(service, h.session, task)
                await service.tick()
            assert (await queue(service, h.session_id))[-1]["status"] == "PENDING"
            assert (await service.get(task["id"]))["status"] == (
                "PAUSED" if manual else "COMPLETED"
            )
            await service.aclose()
            await sessions.aclose()
            await core.shutdown()
            port.release.set()
            core = KernelHostCore.production(model_runtime=runtime)
            sessions = LocalSessionController(
                core=core,
                permission_policy=default_permission_policy(),
                active_skill_names=frozenset(),
            )
            service = ScheduledTaskService(sessions)
            core.scheduled_tasks = service
            await service.tick()

            async def done():
                rows = await queue(service, h.session_id)
                handle = sessions._by_session.get(h.session_id)
                return (
                    rows[-1]["status"] == "CONSUMED"
                    and handle is not None
                    and handle.session._active_task is None
                )

            await settled(done)
            rows = await queue(service, h.session_id)
            assert len(rows) == 2 and rows[-1]["input_origin"] == "SCHEDULED_TASK"
            assert len(port.requests) == 2
            await service.tick()
            assert len(await queue(service, h.session_id)) == 2
        finally:
            port.release.set()
            await service.aclose()
            await sessions.aclose()
            await core.shutdown()

    asyncio.run(scenario())


def test_manual_enqueue_unknown_commit_ack_and_exact_confirmation(
    tmp_path, stage2_migrated_postgres_database, monkeypatch
):
    async def scenario():
        core, sessions, service, h, _ = await setup(
            tmp_path,
            stage2_migrated_postgres_database,
            monkeypatch,
            _DogfoodModelPort(),
        )
        try:
            task = await create(service, h.session)
            original = h.session.repository.enqueue_prompt
            once = True

            def unknown(*args, **kwargs):
                nonlocal once
                result = original(*args, **kwargs)
                if once:
                    once = False
                    raise OSError("lost COMMIT acknowledgement")
                return result

            monkeypatch.setattr(h.session.repository, "enqueue_prompt", unknown)
            request = dict(
                client_command_id=uuid4().hex,
                session_id=h.session_id,
                task_id=task["id"],
                expected_revision=task["revision"],
                request_at_utc=datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat(),
            )
            accepted = await service.run_now(request)

            async def done():
                return (await queue(service, h.session_id))[-1][
                    "status"
                ] == "CONSUMED" and h.session._active_task is None

            await settled(done)
            assert (await service.run_now(request))["queue_item_id"] == accepted[
                "queue_item_id"
            ]
            assert len(await queue(service, h.session_id)) == 1
        finally:
            await service.aclose()
            await sessions.aclose()
            await core.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["edit", "due_changed"])
def test_final_admission_rechecks_cut_after_hook(
    tmp_path, stage2_migrated_postgres_database, monkeypatch, change
):
    async def scenario():
        core, sessions, service, h, _ = await setup(
            tmp_path,
            stage2_migrated_postgres_database,
            monkeypatch,
            _DogfoodModelPort(),
        )
        try:
            task = await create(service, h.session)
            await due(service, h.session, task)
            entered, release = asyncio.Event(), asyncio.Event()
            original = h.session._dispatch_user_prompt_hook

            async def held(**kwargs):
                value = await original(**kwargs)
                entered.set()
                await release.wait()
                return value

            monkeypatch.setattr(h.session, "_dispatch_user_prompt_hook", held)
            dispatch = asyncio.create_task(service.tick())
            await asyncio.wait_for(entered.wait(), 15)
            if change == "edit":
                values = {
                    k: task[k]
                    for k in (
                        "name",
                        "prompt",
                        "schedule",
                        "timezone",
                        "permission_mode",
                    )
                }
                values["prompt"] = "changed after hook"
                changed = await service.mutate(
                    task_id=task["id"],
                    expected_revision=task["revision"],
                    action="update",
                    values=values,
                )
            else:

                def back(*, deadline_monotonic):
                    with h.session.repository._writer_transaction(
                        h.session._lease.guard, deadline_monotonic=deadline_monotonic
                    ) as c:
                        c.execute(
                            "UPDATE pulsara_v3.scheduled_tasks SET next_run_at=clock_timestamp()+interval '1 hour' WHERE id=%s",
                            (task["id"],),
                        )

                await service._io.run(back, deadline_monotonic=monotonic() + 30)
            release.set()
            await dispatch
            assert await queue(service, h.session_id) == []
            current = await service.get(task["id"])
            assert current["status"] == "ACTIVE"
            if change == "edit":
                assert (
                    current["revision"] == changed["revision"]
                    and current["prompt"] == "changed after hook"
                )
            else:
                assert datetime.fromisoformat(current["next_run_at"]) > datetime.now(
                    timezone.utc
                )
        finally:
            await service.aclose()
            await sessions.aclose()
            await core.shutdown()

    asyncio.run(scenario())


def test_scheduled_source_survives_real_compaction_fork_and_parent_deletion(
    tmp_path, stage2_migrated_postgres_database, monkeypatch
):
    async def scenario():
        class SummaryPort(_DogfoodModelPort):
            def __init__(self):
                self.started = asyncio.Event()
                self.release = asyncio.Event()
                self.pausing = None
                super().__init__()

            def __getattr__(self, name):
                return getattr(self._delegate, name)

            async def _stream(self, request):
                if self.pausing is not None:
                    import json
                    from pulsara_agent.ports.live_agent_event import (
                        ToolCallStartPayload,
                        ToolCallDeltaPayload,
                        ToolCallEndPayload,
                        live_digest,
                    )

                    args = json.dumps(self.pausing)
                    self.pausing = None
                    self.started.set()
                    await self.release.wait()
                    yield ToolCallStartPayload(
                        "pause:self", "pause:self", "scheduled_tasks"
                    )
                    yield ToolCallDeltaPayload("pause:self", "pause:self", args)
                    yield ToolCallEndPayload(
                        "pause:self",
                        "pause:self",
                        "scheduled_tasks",
                        args,
                        len(args.encode()),
                        live_digest(args),
                    )
                else:
                    async for item in super()._stream(request):
                        yield item

        port = SummaryPort()
        port.model_runtime = port._delegate.model_runtime
        port.transport_timeout_policy = port._delegate.transport_timeout_policy
        from pulsara_agent.conversation_kernel.compaction.model_call import (
            PreparedCompactionSummaryCall,
            RawCompactionSummaryResponse,
        )
        from pulsara_agent.llm.input import LLMToolCall

        summary_calls = []

        async def summary(call):
            summary_calls.append(call.semantic.semantic_input)
            return RawCompactionSummaryResponse(
                "Prior scheduled reports checked the workspace.",
                (LLMToolCall(id="summary:forbidden", name="terminal", arguments="{}"),)
                if len(summary_calls) == 1
                else (),
            )

        monkeypatch.setattr(PreparedCompactionSummaryCall, "open_once", summary)
        core, sessions, service, h, _ = await setup(
            tmp_path, stage2_migrated_postgres_database, monkeypatch, port
        )
        try:
            task = await create(
                service, h.session, prompt="Saved report context. " * 1500
            )
            for _ in range(4):
                await service.run_now(
                    dict(
                        client_command_id=uuid4().hex,
                        session_id=h.session_id,
                        task_id=task["id"],
                        expected_revision=task["revision"],
                        request_at_utc=datetime.now(timezone.utc)
                        .replace(microsecond=0)
                        .isoformat(),
                    )
                )

                async def done():
                    return (await queue(service, h.session_id))[-1][
                        "status"
                    ] == "CONSUMED" and h.session._active_task is None

                await settled(done)
            from pulsara_agent.terminal_protocol.canonical_v3 import (
                CanonicalProtocolReader,
            )

            original_reader = CanonicalProtocolReader(
                h.session.repository.connection_provider
            )
            original_history = await service._io.run(
                original_reader.snapshot,
                session_id=h.session_id,
                maximum_entries=256,
                maximum_control_items=128,
                deadline_monotonic=monotonic() + 30,
            )
            historical_child = await sessions.fork_conversation(
                h.session_id, anchor_entry_id=original_history.entries[-1].entry_id
            )
            assert historical_child["outcome"] == "CREATED_AND_OPENED"
            values = {
                k: task[k]
                for k in ("name", "prompt", "schedule", "timezone", "permission_mode")
            }
            values["permission_mode"] = "bypass-permissions"
            task = await service.mutate(
                task_id=task["id"],
                expected_revision=task["revision"],
                action="update",
                values=values,
            )
            port.pausing = {
                "action": "pause",
                "task_id": task["id"],
                "expected_revision": task["revision"],
            }
            await service.run_now(
                dict(
                    client_command_id=uuid4().hex,
                    session_id=h.session_id,
                    task_id=task["id"],
                    expected_revision=task["revision"],
                    request_at_utc=datetime.now(timezone.utc)
                    .replace(microsecond=0)
                    .isoformat(),
                )
            )
            await asyncio.wait_for(port.started.wait(), 15)
            compact = asyncio.create_task(
                h.session.compact_context(command_id="scheduled:compact", force=True)
            )

            async def registered():
                return bool(h.session._compaction._manual)

            await settled(registered)
            port.release.set()
            outcome = await compact
            assert outcome.disposition.value == "COMPACTED", outcome

            async def quiet():
                return h.session._active_task is None

            await settled(quiet)
            assert (await service.get(task["id"]))["status"] == "PAUSED"
            assert len(summary_calls) == 2
            assert (
                summary_calls[1].messages[: len(summary_calls[0].messages)]
                == summary_calls[0].messages
            )
            assert summary_calls[1].system_prompt == summary_calls[0].system_prompt
            assert "Runtime scheduled trigger" in str(summary_calls[0].messages)
            human = await h.session.run_turn(
                PromptContent.text("Human continuation after compaction")
            )
            from pulsara_agent.web_app.browser_bridge import LocalBrowserBridge

            bridge = LocalBrowserBridge(sessions=sessions, protocol_server=object())
            creation = await sessions.fork_conversation(
                h.session_id, anchor_entry_id=human.final_entry_id
            )
            assert creation["outcome"] == "CREATED_AND_OPENED", creation
            child = sessions._by_session[creation["child_session_id"]].session
            await sessions.delete_session(h.session_id, bridge=bridge)
            assert (await service.list(session_id=h.session_id))["tasks"] == []
            reply = await child.run_turn(PromptContent.text("Continue in child"))
            assert reply.final_text == "STAGE2_DOGFOOD_OK"
            from pulsara_agent.terminal_protocol.canonical_v3 import (
                CanonicalProtocolReader,
            )

            reader = CanonicalProtocolReader(child.repository.connection_provider)
            snap = await service._io.run(
                reader.snapshot,
                session_id=child.session_id,
                maximum_entries=256,
                maximum_control_items=128,
                deadline_monotonic=monotonic() + 30,
            )
            # A pre-compaction fork owns copied typed rows; a post-compaction
            # fork owns the snapshot plus its suffix, and source is in the carrier.
            before_child = sessions._by_session[
                historical_child["child_session_id"]
            ].session
            snap = await service._io.run(
                CanonicalProtocolReader(
                    before_child.repository.connection_provider
                ).snapshot,
                session_id=before_child.session_id,
                maximum_entries=256,
                maximum_control_items=128,
                deadline_monotonic=monotonic() + 30,
            )
            scheduled = [e for e in snap.entries if e.HasField("scheduled_input")]
            assert scheduled and all(
                e.scheduled_input.task_id == task["id"] for e in scheduled
            )
            from pulsara_agent.conversation_kernel.reader import (
                CanonicalProviderInputReader,
            )

            canonical = await service._io.run(
                CanonicalProviderInputReader(
                    child.repository.connection_provider
                ).read_frozen_compaction_cut,
                port._delegate.requests[-1].cut,
                deadline_monotonic=monotonic() + 30,
            )
            retained = canonical.snapshot_carrier.retained_historical_requests
            assert any(
                r.input_origin.value == "SCHEDULED_TASK"
                and r.scheduled_input.task_id == task["id"]
                for r in retained
            )
            assert all(
                r.input_origin.value != "SCHEDULED_TASK"
                for r in canonical.snapshot_carrier.recent_human_requests
            )
        finally:
            await service.aclose()
            await sessions.aclose()
            await core.shutdown()

    asyncio.run(scenario())
