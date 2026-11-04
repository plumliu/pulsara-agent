"""Scheduler composes with the real Host/controller and ordinary ROOT queue."""

from __future__ import annotations
from pulsara_agent.identity import random_id
import asyncio
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from time import monotonic
import shutil
import pytest
from psycopg.rows import dict_row
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.llm.input import FrozenPromptContent, PromptContent
from pulsara_agent.scheduling.contracts import ScheduledTriggerSource
from pulsara_agent.tool_permission import default_permission_policy
from pulsara_agent.desktop_app.session_controller import LocalSessionController
from pulsara_agent.conversation_kernel.host import SessionControlRejected
from pulsara_agent.llm.model_connections import ModelCallBinding, ModelConnectionId
from pulsara_agent.workspace_identity import WorkspaceUnavailable
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.support.model_config import test_model_runtime, test_model_binding
from tests.test_stage2_kernel_host_dogfood import _SteerModelPort, _DogfoodModelPort

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("desktop", [False, True])
def test_complete_core_schedule_tools_execute_and_user_management_needs_no_ui_session(
    tmp_path, stage2_migrated_postgres_database, monkeypatch, desktop,
):
    async def scenario():
        import pulsara_agent.conversation_kernel.host as host_module
        from pulsara_agent.model_input.contracts import ModelInputScopeKind
        from pulsara_agent.primitives.permission import PermissionMode
        from pulsara_agent.tool_permission import preset_to_policy
        from pulsara_agent.workspace_identity import HostWorkspaceInput
        from tests.support.round3 import invoke_direct_tool

        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        monkeypatch.setattr(host_module.LocalMcpManagementService, "load_configs", lambda *a, **k: ())
        root = tmp_path / "complete-core"
        root.mkdir()
        runtime = test_model_runtime(postgres_dsn=stage2_migrated_postgres_database.runtime_dsn)
        policy = preset_to_policy(PermissionMode.BYPASS_PERMISSIONS)
        domain = "schedule-desktop-user" if desktop else "schedule-direct-user"
        core = KernelHostCore.production(model_runtime=runtime, permission_policy=policy, memory_domain_id=domain)
        controller = LocalSessionController(core=core, permission_policy=policy, memory_domain_id=domain, active_skill_names=frozenset()) if desktop else None
        other = KernelHostCore.production(model_runtime=runtime, memory_domain_id="another-user")
        try:
            if controller is None:
                session = await core.open_session(HostWorkspaceInput(workspace_kind="project", workspace_root=root, memory_domain_id=domain))
            else:
                session = (await controller.create_session(workspace_kind="project", workspace_path=str(root))).session
            await session.update_model_call_binding(test_model_binding(runtime))
            assert session._tools._scheduled_tasks is core.scheduled_tasks
            bindings = session._tools.sealed_builtin_capability_snapshot(
                conversation_scope_kind=ModelInputScopeKind.ROOT,
                effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            ).executor_bindings
            names = {binding.tool_name for binding in bindings}
            assert len(names) == 39
            assert {"read_video_frames", "read_transcript", "annotate_media"} <= names
            assert {"query_schedules", "manage_schedule"} <= names

            async def invoke(name, arguments, target_session=None):
                target_session = target_session or session
                result = await invoke_direct_tool(target_session._tools, session_id=target_session.session_id,
                    workspace_id=target_session.workspace.workspace_key, tool_name=name, arguments=arguments,
                    tool_call_id=random_id("call"), attempt_id=random_id("attempt"),
                    turn_id=random_id("turn"), assistant_entry_id=random_id("entry"))
                assert result.state == "SUCCESS", result.public_value()
                return result.public_value()

            arguments = {"action": "create", "values": {
                "name": "Core-owned report", "prompt": "Inspect current changes.", "timezone": "UTC",
                "permission_mode": "ask-permissions", "schedule": {
                    "contract": "scheduled-rule:v1", "kind": "interval", "seconds": 60,
                    "anchor_at_utc": "2030-01-01T00:00:00Z",
                },
            }}
            created = await invoke("manage_schedule", arguments)
            page = await invoke("query_schedules", {})
            assert [task["target"] for task in page["tasks"]] == [created["target"]]
            task_id = created["target"]["task_id"]
            # A legitimate explicit ROOT domain is carried by its immutable
            # Host binding, never supplied as a model argument or inferred from
            # the currently selected desktop session.
            separate = await core.open_session(HostWorkspaceInput(workspace_kind="project",
                workspace_root=root, memory_domain_id=f"{domain}-explicit-root"))
            await separate.update_model_call_binding(test_model_binding(runtime))
            separate_created = await invoke("manage_schedule", arguments, separate)
            own_page, separate_page = await asyncio.gather(
                invoke("query_schedules", {}), invoke("query_schedules", {}, separate))
            assert [item["target"] for item in own_page["tasks"]] == [created["target"]]
            assert [item["target"] for item in separate_page["tasks"]] == [separate_created["target"]]
            await core.close_session(separate.host_session_id)
            await core.close_session(session.host_session_id)
            assert not core._sessions
            listed = await core.scheduled_tasks.list()
            assert [task["id"] for task in listed["tasks"]] == [task_id]
            changed = await core.scheduled_tasks.mutate(task_id=task_id,
                expected_revision=created["revision"], action="pause")
            assert changed["status"] == "PAUSED"
            assert not core._sessions
            assert (await other.scheduled_tasks.list())["tasks"] == []
        finally:
            if controller is not None:
                await controller.aclose()
            await other.shutdown()
            await core.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["initialize", "discovery"])
@pytest.mark.parametrize("finish", ["ready", "stop", "close"])
def test_canonical_scheduled_pending_restore_waits_before_actual_provider_and_prefix(
    tmp_path, stage2_migrated_postgres_database, monkeypatch, phase, finish,
):
    async def scenario():
        from pulsara_agent.conversation_kernel import host as host_module
        from pulsara_agent.conversation_kernel.host import KernelHostSession
        from pulsara_agent.conversation_kernel.mcp import supervisor as mcp_module
        from pulsara_agent.llm.adapters.openai.chat_completions import (
            OpenAIChatCompletionsTransport, build_chat_completions_payload,
        )
        from pulsara_agent.primitives.model_call import ModelCallPurpose
        from pulsara_agent.workspace_identity import HostWorkspaceInput
        from tests.test_mcp_first_request_readiness import controlled_mcp_client, readiness_config

        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        runtime = test_model_runtime(wire_api="openai_chat_completions",
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn)
        workspace_input = HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path,
            memory_domain_id=f"scheduled-pending-{phase}-{finish}")
        original_stream = OpenAIChatCompletionsTransport.stream
        requests = []
        provider_sent = asyncio.Event()
        async def stream(adapter, *, call, context):
            if call.fact.purpose is ModelCallPurpose.AGENT_MODEL_LOOP:
                requests.append(build_chat_completions_payload(call=call, context=context))
                provider_sent.set()
            adapter._mock_chunks = [
                {"choices": [{"index": 0, "delta": {"content": "SCHEDULE COMPLETED"}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ]
            async for event in original_stream(adapter, call=call, context=context):
                yield event
        monkeypatch.setattr(OpenAIChatCompletionsTransport, "stream", stream)
        with monkeypatch.context() as seed:
            seed.setattr(host_module.LocalMcpManagementService, "load_configs", lambda *a, **k: ())
            seed.setattr(KernelHostSession, "activate_execution", lambda self: None)
            core = KernelHostCore.production(model_runtime=runtime, memory_domain_id=workspace_input.memory_domain_id)
            session = await core.open_session(workspace_input)
            session_id = session.session_id
            await session.update_model_call_binding(test_model_binding(runtime))
            task = await create(core.scheduled_tasks, session, once=True)
            request = {"client_command_id": random_id("command"), "session_id": session_id,
                "task_id": task["id"], "expected_revision": task["revision"],
                "request_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
            accepted = await core.scheduled_tasks.run_now(request)
            assert (await session.query_command(request["client_command_id"])).status == "PENDING"
            assert not requests
            await core.shutdown()

        Client, entered, release = controlled_mcp_client(phase)
        async def factory():
            return Client
        monkeypatch.setattr(mcp_module, "_prepare_sdk_client_factory", factory)
        monkeypatch.setattr(host_module.LocalMcpManagementService, "load_configs",
            lambda *a, **k: (readiness_config(),))
        core = KernelHostCore.production(model_runtime=runtime, memory_domain_id=workspace_input.memory_domain_id)
        try:
            await core.scheduled_tasks.tick()
            session = core._live_agent_session(session_id)
            assert session is not None
            assert core._sessions[session.host_session_id] is session
            assert session._delivery_task is not None
            await asyncio.wait_for(entered.wait(), 5)
            async with asyncio.timeout(10):
                while session.mcp_readiness_progress() is None:
                    await asyncio.sleep(0)
            assert session.mcp_readiness_progress()["waiting"] is True
            scope = next(iter(session._input_continuity._slots))
            assert session._input_continuity.current_view(scope) is None
            assert requests == []
            attempt = session._mcp_supervisor._tasks["fixture"]
            if finish == "stop":
                await session.request_stop_turn(command_id=random_id("command"),
                    admission_deadline_ms=int(datetime.now(timezone.utc).timestamp() * 1000) + 30_000,
                    expected_session_id=session.session_id, expected_host_session_id=session.host_session_id,
                    target_turn_id=session.active_root_turn_id())
                assert not attempt.done()
            elif finish == "close":
                await core.close_session(session.host_session_id)
                assert attempt.done()
            release.set()
            if finish == "ready":
                await asyncio.wait_for(provider_sent.wait(), 10)
                async with asyncio.timeout(10):
                    while (await session.query_command(request["client_command_id"])).status == "PENDING":
                        await asyncio.sleep(0)
                assert len(requests) == 1
                assert any(tool["function"]["name"] == "mcp__fixture__fake_echo" for tool in requests[0]["tools"])
                confirmed = await core.scheduled_tasks.run_now(request)
                assert confirmed["queue_item_id"] == accepted["queue_item_id"]
                assert confirmed["execution"]["status"] == "completed"
                assert len(requests) == 1
            else:
                if not attempt.done():
                    await asyncio.wait_for(attempt, 5)
                assert requests == []
                assert session._input_continuity.current_view(scope) is None
        finally:
            release.set()
            await core.shutdown()
    asyncio.run(scenario())


def test_shutdown_during_last_open_io_cannot_construct_an_unregistered_session(
    tmp_path, stage2_migrated_postgres_database, monkeypatch,
):
    async def scenario():
        from threading import Event
        from pulsara_agent.conversation_kernel import host as host_module
        from pulsara_agent.workspace_identity import HostWorkspaceInput
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        monkeypatch.setattr(host_module.LocalMcpManagementService, "load_configs", lambda *a, **k: ())
        core = KernelHostCore.production(model_runtime=test_model_runtime(
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn))
        entered, release = Event(), Event()
        original_read = host_module.ConversationKernelRepository.read_session_root_id
        constructed = []
        original_construct = host_module.KernelHostSession.__init__
        def read(*args, **kwargs):
            entered.set()
            assert release.wait(10), "test did not release final physical open read"
            return original_read(*args, **kwargs)
        def construct(session, **kwargs):
            constructed.append(kwargs["host_session_id"])
            return original_construct(session, **kwargs)
        monkeypatch.setattr(host_module.ConversationKernelRepository, "read_session_root_id", read)
        monkeypatch.setattr(host_module.KernelHostSession, "__init__", construct)
        stopping = asyncio.Event()
        original_stop = core.scheduled_tasks.stop
        async def stop():
            stopping.set()
            await original_stop()
        monkeypatch.setattr(core.scheduled_tasks, "stop", stop)
        opening = asyncio.create_task(core.open_session(HostWorkspaceInput(
            workspace_kind="project", workspace_root=tmp_path)))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            close = asyncio.create_task(core.shutdown())
            await stopping.wait()
            assert core._closing
            attempt = next(item for item in core._open_attempts.values() if item.configuration is not None)
            assert attempt.candidate_session is None
            assert attempt.io_owner.physical_operation_deadline is not None
            assert core._session_open_children_stopped()
            assert not constructed
            release.set()
            with pytest.raises(host_module.KernelHostCoreClosing):
                await opening
            await close
            assert not constructed and not core._sessions and not core._open_attempts
            assert not core.has_database_resources
        finally:
            release.set()
            await asyncio.gather(opening, return_exceptions=True)
            await core.shutdown()
    asyncio.run(scenario())


async def settled(check):
    # Test observation timeout, not a production turn/task lifetime bound.
    async with asyncio.timeout(15):
        while not await check():
            await asyncio.sleep(0.01)


async def setup(tmp_path, db, monkeypatch, port, *, runtime=None):
    import pulsara_agent.conversation_kernel.host as host

    monkeypatch.setattr(host, "DirectKernelModelPort", lambda **_: port)
    monkeypatch.setattr(
        host.LocalMcpManagementService, "load_configs", lambda *a, **k: ()
    )
    runtime = runtime or test_model_runtime(postgres_dsn=db.runtime_dsn)
    root = tmp_path / "workspace"
    root.mkdir()
    core = KernelHostCore.production(model_runtime=runtime)
    sessions = LocalSessionController(
        core=core,
        permission_policy=default_permission_policy(),
        active_skill_names=frozenset(),
    )
    service = core.scheduled_tasks
    handle = await sessions.create_session(
        workspace_kind="project", workspace_path=str(root)
    )
    await handle.session.update_model_call_binding(test_model_binding(runtime))
    return core, sessions, service, handle, root


async def create(service, session, prompt="Scheduled report", once=False):
    schedule = {
        "contract": 'scheduled-rule:v1',
        "kind": "interval",
        "anchor_at_utc": "2020-01-01T00:00:00+00:00",
        "seconds": 60,
    }
    if once:
        schedule = {
            "contract": 'scheduled-rule:v1',
            "kind": "once",
            "run_at_utc": (datetime.now(timezone.utc) + timedelta(hours=1))
            .replace(microsecond=0)
            .isoformat(),
        }
    return await service.create(
        status="ACTIVE",
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
                        ModelConnectionId('connection:eqav4ryyuefvvlc4wstluyas4e'), None
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
                        "contract": 'scheduled-rule:v1',
                        "kind": "daily",
                        "start_date": "2026-10-05",
                        "time": "08:00",
                    },
                    timezone="Asia/Shanghai",
                    permission_mode="bypass-permissions",
                ),
            )
            assert task["status"] == "PAUSED"
            assert task["next_run_at"] is None
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


async def due(service, session, task, *, seconds_ago=1):
    repo = await service._repository()

    # Integral UTC is the schedule protocol. Clock seam changes only the saved due.
    def write(*, deadline_monotonic):
        with repo._writer_transaction(
            session._lease.guard, deadline_monotonic=deadline_monotonic
        ) as c:
            row = c.execute(
                """UPDATE pulsara_v3.scheduled_tasks
                SET next_run_at=date_trunc('second',clock_timestamp())-(%s * interval '1 second')
                WHERE id=%s RETURNING next_run_at""",
                (seconds_ago, task["id"]),
            ).fetchone()
            # This seam represents a due reached during the running kernel.
            service._run_started_at = min(service._run_started_at, row["next_run_at"] - timedelta(seconds=1))

    await service._io.run(write, deadline_monotonic=monotonic() + 30)


async def queue(service, sid):
    def read(*, deadline_monotonic):
        repo = service._core._repository
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


def test_busy_timer_occurrences_stay_independent_and_pause_preserves_steer(
    tmp_path, stage2_migrated_postgres_database, monkeypatch
):
    async def scenario():
        port = _SteerModelPort()
        core, sessions, service, h, _ = await setup(
            tmp_path, stage2_migrated_postgres_database, monkeypatch, port
        )
        try:
            await h.session.submit_prompt(
                command_id='command:36hwiobu4cenlsvnsd33wfvsyi', content=PromptContent.text("Human first")
            )
            await asyncio.wait_for(port.started.wait(), 15)
            task = await create(service, h.session)
            await due(service, h.session, task, seconds_ago=3)
            await service._dispatch(await service._read("read_scheduled_task", task_id=task["id"]))
            await h.session.submit_prompt(
                command_id='command:ag24n2g2ztjbupgbndkdb34zne', content=PromptContent.text("Human second")
            )
            await due(service, h.session, task)
            await service._dispatch(await service._read("read_scheduled_task", task_id=task["id"]))
            rows = await queue(service, h.session_id)
            assert [r["input_origin"] for r in rows] == [
                "HUMAN_MESSAGE",
                "SCHEDULED_TASK",
                "HUMAN_MESSAGE",
                "SCHEDULED_TASK",
            ]
            assert [r["status"] for r in rows] == ["CONSUMED", "PENDING", "PENDING", "PENDING"]
            assert rows[1]["delivery_mode"] == rows[3]["delivery_mode"] == "STEER_ACTIVE_TURN"
            assert rows[1]["target_turn_id"] == rows[3]["target_turn_id"]
            assert rows[1]["scheduled_due_at"] != rows[3]["scheduled_due_at"]
            assert len(port.requests) == 1
            paused = await service.mutate(
                task_id=task["id"], expected_revision=1, action="pause"
            )
            assert paused["status"] == "PAUSED"
            assert (await queue(service, h.session_id))[1]["status"] == "PENDING"
            assert (await queue(service, h.session_id))[3]["status"] == "PENDING"
            port.release.set()

            async def done():
                return all(row["status"] == "CONSUMED" for row in await queue(service, h.session_id)) and h.session._active_task is None

            await settled(done)
            assert len(port.requests) == 3
            # Explicit run uses the same paused task, without changing its plan.
            req = dict(
                client_command_id=random_id("command"),
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
                and last.scheduled_input.trigger_source
                is ScheduledTriggerSource.RUN_ONCE
            )
            await service.mutate(
                task_id=task["id"],
                expected_revision=paused["revision"],
                action="delete",
            )
            confirmed = await service.run_now(req)
            assert (
                confirmed["queue_item_id"] == accepted["queue_item_id"]
                and confirmed["execution"] == {"status": "completed", "terminal_reason": None}
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
            shutil.rmtree(root)
            await service.tick()
            paused = await service.get(task["id"])
            assert paused["status"] == "PAUSED" and not root.exists()
            req = dict(
                client_command_id=random_id("command"),
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
def test_restart_wakes_removed_once_pending_from_timer_or_user(
    tmp_path, stage2_migrated_postgres_database, monkeypatch, manual
):
    async def scenario():
        port = _SteerModelPort()
        core, sessions, service, h, _ = await setup(
            tmp_path, stage2_migrated_postgres_database, monkeypatch, port
        )
        runtime = core._model_runtime
        try:
            # Keep an accepted NEW_TURN durable while deliberately withholding
            # the process-local consumer; no RUNNING turn exists to steer.
            h.session._delivery_task.cancel()
            await asyncio.gather(h.session._delivery_task, return_exceptions=True)
            task = await create(service, h.session, once=True)
            if manual:
                task = await service.mutate(
                    task_id=task["id"],
                    expected_revision=task["revision"],
                    action="pause",
                )
                await service.run_now(
                    dict(
                        client_command_id=random_id("command"),
                        session_id=h.session_id,
                        task_id=task["id"],
                        expected_revision=task["revision"],
                        request_at_utc=datetime.now(timezone.utc)
                        .replace(microsecond=0)
                        .isoformat(),
                    ),
                )
            else:
                await due(service, h.session, task)
                await service.tick()
            assert (await queue(service, h.session_id))[-1]["status"] == "PENDING"
            with pytest.raises(KeyError):
                await service.get(task["id"])
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
            service = core.scheduled_tasks
            await service.tick()

            async def done():
                rows = await queue(service, h.session_id)
                host = core._live_agent_session(h.session_id)
                return (
                    rows[-1]["status"] == "CONSUMED"
                    and host is not None
                    and host._active_task is None
                )

            await settled(done)
            rows = await queue(service, h.session_id)
            assert len(rows) == 1 and rows[-1]["input_origin"] == "SCHEDULED_TASK"
            assert not sessions._by_session
            assert len(port.requests) == 1
            await service.tick()
            assert len(await queue(service, h.session_id)) == 1
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
            task = await create(service, h.session, once=True)
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
                client_command_id=random_id("command"),
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
def test_final_admission_rechecks_task_cut_before_writer(
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
            from threading import Event
            entered, release = asyncio.Event(), Event()
            loop = asyncio.get_running_loop()
            original = h.session.repository.enqueue_prompt

            def held(*args, **kwargs):
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(15), "test did not release admission writer barrier"
                return original(*args, **kwargs)

            monkeypatch.setattr(h.session.repository, "enqueue_prompt", held)
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
        from pulsara_agent.conversation_kernel.compaction.contracts import (
            ResolvedCompactionPolicy,
        )
        from tests.test_round9_2_hook_integration_postgres import (
            _CompactionSummaryExecution,
            _tool_stream,
        )

        class SummaryTransport:
            def __init__(self):
                self.calls = []

            def open_stream(self, *, call, context):
                del call
                self.calls.append(context)
                execution = _CompactionSummaryExecution(
                    "Prior scheduled reports checked the workspace."
                )
                if len(self.calls) == 1:
                    execution._items[:0] = _tool_stream(
                        "exec_process", "summary:forbidden", {}
                    )
                return execution

        class SummaryPort(_DogfoodModelPort):
            def __init__(self):
                self.started = asyncio.Event()
                self.release = asyncio.Event()
                self.pausing = None
                self.summary = SummaryTransport()
                super().__init__()

            def resolve_compaction_summary_call(self, **kwargs):
                call = self._delegate.resolve_compaction_summary_call(**kwargs)
                self.summary.binding_id = call.target.transport.binding_id
                self.summary.contract_version = call.target.transport.contract_version
                call = replace(call, target=replace(call.target, transport=self.summary))
                self.model_runtime.register_test_call_override(call)
                return call

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
                        "pause:self", "pause:self", "manage_schedule"
                    )
                    yield ToolCallDeltaPayload("pause:self", "pause:self", args)
                    yield ToolCallEndPayload(
                        "pause:self",
                        "pause:self",
                        "manage_schedule",
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
        summary_calls = port.summary.calls
        core, sessions, service, h, _ = await setup(
            tmp_path, stage2_migrated_postgres_database, monkeypatch, port,
            runtime=test_model_runtime(
                postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
                model_id="test-pro", wire_api="openai_chat_completions",
            ),
        )
        from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort

        port._delegate._model_runtime = core._model_runtime
        port._delegate._preparer = DirectKernelModelPort(model_runtime=core._model_runtime)
        port.model_runtime = core._model_runtime
        try:
            h.session._compaction.policy = ResolvedCompactionPolicy(
                automatic_enabled=False, minimum_reclaim_tokens=1
            )
            task = await create(
                service, h.session, prompt="Saved report context. " * 1500
            )
            for _ in range(4):
                await service.run_now(
                    dict(
                        client_command_id=random_id("command"),
                        session_id=h.session_id,
                        task_id=task["id"],
                        expected_revision=task["revision"],
                        request_at_utc=datetime.now(timezone.utc)
                        .replace(microsecond=0)
                        .isoformat(),
                    ),
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
            usage = await h.session.read_context_usage()
            ratio = float(usage["input_tokens"]) / int(usage["input_budget_tokens"])
            h.session._compaction.policy = replace(
                h.session._compaction.policy,
                automatic_enabled=True,
                auto_trigger_ratio=ratio * 0.95,
                post_compaction_target_ratio=ratio * 0.85,
            )
            port.pausing = {
                "action": "pause",
                "target": {"kind": "SCHEDULE", "task_id": task["id"]},
                "expected_revision": task["revision"],
            }
            await service.run_now(
                dict(
                    client_command_id=random_id("command"),
                    session_id=h.session_id,
                    task_id=task["id"],
                    expected_revision=task["revision"],
                    request_at_utc=datetime.now(timezone.utc)
                    .replace(microsecond=0)
                    .isoformat(),
                ),
            )
            await asyncio.wait_for(port.started.wait(), 15)
            # Automatic summary and its repair finish before this scheduled
            # request's first ordinary call can issue the Schedule pause tool.
            assert len(summary_calls) == 2
            active_turn = h.session.active_root_turn_id()
            assert active_turn is not None
            from tests.test_compaction_request_start_integration import _rows

            adopted = _rows(
                h.session,
                "SELECT r.revision_ordinal FROM pulsara_v3.turns t "
                "JOIN pulsara_v3.turn_context_binding_revisions r "
                "ON r.session_id=t.session_id "
                "AND r.id=t.current_context_binding_revision_id "
                "WHERE t.session_id=%s AND t.id=%s",
                (h.session_id, active_turn),
            )[0]
            assert adopted["revision_ordinal"] == 1
            assert (await service.get(task["id"]))["status"] == "ACTIVE"
            port.release.set()

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
            assert task["id"] in str(summary_calls[0].messages)
            assert "User selected Run once" in str(summary_calls[0].messages)
            human = await h.session.run_turn(
                PromptContent.text("Human continuation after compaction")
            )
            from pulsara_agent.desktop_app.renderer_bridge import RendererBridge

            bridge = RendererBridge(sessions=sessions, protocol_server=object())
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
            # The forks preserve typed scheduled provenance in copied history
            # and in the exact current-request suffix after the snapshot.
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
                e.scheduled_input.task_id == task["id"]
                and e.scheduled_input.trigger_source == "RUN_ONCE"
                for e in scheduled
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
            facts = canonical.dispatch_read.compile_snapshot
            scheduled_suffix = tuple(
                item for item in facts.canonical_input.items
                if item.input_origin is not None
                and item.input_origin.value == "SCHEDULED_TASK"
            )
            assert len(scheduled_suffix) == 1
            scheduled_request = scheduled_suffix[0]
            assert scheduled_request.scheduled_input.task_id == task["id"]
            assert (
                scheduled_request.scheduled_input.trigger_source
                is ScheduledTriggerSource.RUN_ONCE
            )
            assert scheduled_request.content == FrozenPromptContent.text(task["prompt"]).parts
            assert (
                scheduled_request.source_entry_sequence
                > facts.context_binding_fact.source_through_sequence
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


@pytest.mark.parametrize("source", list(ScheduledTriggerSource))
def test_saved_task_sources_do_not_trigger_automatic_memory_recall(
    tmp_path, stage2_migrated_postgres_database, monkeypatch, source
):
    async def scenario():
        core, sessions, service, h, _ = await setup(
            tmp_path,
            stage2_migrated_postgres_database,
            monkeypatch,
            _DogfoodModelPort(),
        )
        try:
            task = await create(service, h.session, once=True)

            async def forbidden(*args, **kwargs):
                raise AssertionError(
                    "saved task must not trigger automatic memory recall"
                )

            monkeypatch.setattr(
                h.session._memory_tools, "freeze_automatic_recall_source", forbidden
            )
            if source is ScheduledTriggerSource.TIMER:
                await due(service, h.session, task)
                await service.tick()
            else:
                await service.run_now(
                    dict(
                        client_command_id=random_id("command"),
                        session_id=h.session_id,
                        task_id=task["id"],
                        expected_revision=task["revision"],
                        request_at_utc=datetime.now(timezone.utc)
                        .replace(microsecond=0)
                        .isoformat(),
                    ),
                )

            async def done():
                rows = await queue(service, h.session_id)
                return (
                    bool(rows)
                    and rows[-1]["status"] == "CONSUMED"
                    and h.session._active_task is None
                )

            await settled(done)
            assert (await queue(service, h.session_id))[-1][
                "scheduled_trigger_source"
            ] == source.value
            with pytest.raises(KeyError):
                await service.get(task["id"])
        finally:
            await service.aclose()
            await sessions.aclose()
            await core.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("active", [False, True])
def test_scheduled_hook_gate_belongs_to_new_turn_consumption_and_keeps_route(
    tmp_path, stage2_migrated_postgres_database, monkeypatch, active
):
    async def scenario():
        port = _SteerModelPort()
        core, sessions, service, handle, _ = await setup(
            tmp_path, stage2_migrated_postgres_database, monkeypatch, port
        )
        entered, release = asyncio.Event(), asyncio.Event()
        try:
            if active:
                await handle.session.submit_prompt(
                    command_id=random_id("command"), content=PromptContent.text("Running human request")
                )
                await asyncio.wait_for(port.started.wait(), 15)
            task = await create(service, handle.session)

            async def block(**kwargs):
                if active:
                    raise AssertionError("scheduled steer cannot run a new-turn Hook")
                assert kwargs["input_origin"] == "SCHEDULED_TASK"
                entered.set()
                await release.wait()
                return "scheduled NEW blocked by UserPromptSubmit", None

            monkeypatch.setattr(handle.session, "_dispatch_user_prompt_hook", block)
            request = dict(
                client_command_id=random_id("command"), session_id=handle.session_id,
                task_id=task["id"], expected_revision=task["revision"],
                request_at_utc=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            )
            result = await service.run_now(request)
            original = (await queue(service, handle.session_id))[-1]
            assert original["status"] == "PENDING"
            assert original["scheduled_trigger_source"] == "RUN_ONCE"
            if active:
                assert result["delivery_mode"] == "STEER_ACTIVE_TURN"
                assert result["target_turn_id"] is not None and result["execution"] is None
                assert not entered.is_set()
            else:
                await asyncio.wait_for(entered.wait(), 15)
                assert result["delivery_mode"] == "NEW_TURN" and result["target_turn_id"] is None
                assert result["execution"]["status"] == "queued"
                release.set()

                async def rejected():
                    return (await queue(service, handle.session_id))[-1]["status"] == "REJECTED"

                await settled(rejected)
                confirmed = await service.run_now(request)
                assert confirmed["queue_item_id"] == result["queue_item_id"]
                assert confirmed["delivery_mode"] == "NEW_TURN" and confirmed["target_turn_id"] is None
                assert confirmed["execution"] == {"status": "stopped", "terminal_reason": "HOOK_BLOCKED_BEFORE_DELIVERY"}
                final = (await queue(service, handle.session_id))[-1]
                assert final["inline_content"] == original["inline_content"]
                assert final["consumed_entry_id"] is None
                assert not port.requests
        finally:
            release.set()
            port.release.set()
            await service.aclose()
            await sessions.aclose()
            await core.shutdown()

    asyncio.run(scenario())


async def expire_paused_once_in_host(service, session, task):
    from psycopg.types.json import Jsonb

    schedule = {**task["schedule"], "run_at_utc": (
        datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=1)
    ).isoformat()}
    repo = await service._repository()

    def expire(*, deadline_monotonic):
        with repo._writer_transaction(session._lease.guard, deadline_monotonic=deadline_monotonic) as connection:
            connection.execute("UPDATE pulsara_v3.scheduled_tasks SET schedule=%s WHERE id=%s", (Jsonb(schedule), task["id"]))

    await service._io.run(expire, deadline_monotonic=monotonic() + 30)


@pytest.mark.parametrize("restart", [False, True])
def test_paused_once_expires_quietly_in_running_kernel_and_after_restart(
    tmp_path, stage2_migrated_postgres_database, monkeypatch, restart
):
    async def scenario():
        port = _SteerModelPort()
        core, sessions, service, handle, _ = await setup(tmp_path, stage2_migrated_postgres_database, monkeypatch, port)
        try:
            once = await create(service, handle.session, once=True)
            paused = await service.mutate(task_id=once["id"], expected_revision=once["revision"], action="pause")
            periodic = await create(service, handle.session)
            periodic = await service.mutate(task_id=periodic["id"], expected_revision=periodic["revision"], action="pause")
            if not restart:
                await service.start()
            await expire_paused_once_in_host(service, handle.session, paused)
            if restart:
                runtime = core._model_runtime
                await service.aclose()
                await sessions.aclose()
                await core.shutdown()
                core = KernelHostCore.production(model_runtime=runtime)
                sessions = LocalSessionController(core=core, permission_policy=default_permission_policy(), active_skill_names=frozenset())
                service = core.scheduled_tasks
                await service.start()
            else:
                service.wake()

            async def missing():
                return await service._read("read_scheduled_task", task_id=paused["id"]) is None

            await settled(missing)
            assert (await service.get(periodic["id"]))["status"] == "PAUSED"
            assert not await queue(service, handle.session_id)
            assert not port.requests
        finally:
            await service.aclose()
            await sessions.aclose()
            await core.shutdown()

    asyncio.run(scenario())


def test_manual_run_crossing_paused_once_deadline_is_rejected_by_final_writer(
    tmp_path, stage2_migrated_postgres_database, monkeypatch
):
    async def scenario():
        from threading import Event
        from pulsara_agent.scheduling.contracts import ScheduledTaskError

        port = _SteerModelPort()
        core, sessions, service, handle, _ = await setup(tmp_path, stage2_migrated_postgres_database, monkeypatch, port)
        entered, release = asyncio.Event(), Event()
        loop = asyncio.get_running_loop()
        try:
            once = await create(service, handle.session, once=True)
            paused = await service.mutate(task_id=once["id"], expected_revision=once["revision"], action="pause")
            original = handle.session.repository.enqueue_prompt

            def held(*args, **kwargs):
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(15), "test did not release final writer"
                return original(*args, **kwargs)

            monkeypatch.setattr(handle.session.repository, "enqueue_prompt", held)
            request = dict(client_command_id=random_id("command"), session_id=handle.session_id,
                task_id=paused["id"], expected_revision=paused["revision"],
                request_at_utc=datetime.now(timezone.utc).replace(microsecond=0).isoformat())
            pending = asyncio.create_task(service.run_now(request))
            await asyncio.wait_for(entered.wait(), 15)
            await expire_paused_once_in_host(service, handle.session, paused)
            release.set()
            with pytest.raises(ScheduledTaskError) as error:
                await pending
            assert error.value.code == "TASK_NOT_FOUND"
            assert await service._read("read_scheduled_task", task_id=paused["id"]) is None
            assert not await queue(service, handle.session_id)
            assert not port.requests
        finally:
            release.set()
            await service.aclose()
            await sessions.aclose()
            await core.shutdown()

    asyncio.run(scenario())
