"""Actual workspace disappearance, cold reads and existing writer restoration."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import shutil
from time import monotonic
from uuid import uuid4

import pytest

from pulsara_agent.conversation_kernel.host import (
    KernelHostCore,
    _restore_workspace_directory,
)
from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
from pulsara_agent.conversation_kernel.repository_errors import SessionWriterConflict
from pulsara_agent.conversation_kernel.workspace import WorkspaceExecutionGate
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from pulsara_agent.ports.user_control_feedback import (
    WorkspaceRecreatedFeedbackContent,
    project_user_control_feedback_for_provider,
)
from pulsara_agent.conversation_kernel.host import default_permission_policy
from pulsara_agent.web_app.browser_bridge import LocalBrowserBridge
from pulsara_agent.web_app.session_controller import LocalSessionController
from pulsara_agent.workspace_identity import (
    HostWorkspaceInput,
    WorkspaceUnavailable,
    observe_workspace,
    resolve_workspace,
)
from tests.support.model_config import test_model_runtime, test_model_binding
from tests.support.postgres import verified_postgres_provider
from tests.test_stage2_kernel_host_dogfood import _DogfoodModelPort, _SteerModelPort


@pytest.mark.parametrize("kind", ["project", "transient"])
def test_existing_workspace_never_creates_or_rebinds(kind, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    source = HostWorkspaceInput(workspace_kind=kind, workspace_root=root)
    original = resolve_workspace(source)
    root.rmdir()
    with pytest.raises(WorkspaceUnavailable) as caught:
        resolve_workspace(source, intent="EXISTING")
    assert caught.value.observation.outcome == "MISSING"
    assert not root.exists()
    assert _restore_workspace_directory(root)
    assert (
        resolve_workspace(source, intent="EXISTING").workspace_key
        == original.workspace_key
    )
    (root / "external.txt").write_text("preserve")
    assert not _restore_workspace_directory(root)
    assert (root / "external.txt").read_text() == "preserve"
    shutil.rmtree(root)
    root.write_text("collision")
    assert observe_workspace(root).outcome == "UNAVAILABLE"
    with pytest.raises(WorkspaceUnavailable):
        _restore_workspace_directory(root)
    assert root.read_text() == "collision"
    root.unlink()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    root.symlink_to(elsewhere, target_is_directory=True)
    assert observe_workspace(root).outcome == "UNAVAILABLE"
    with pytest.raises(WorkspaceUnavailable):
        resolve_workspace(source, intent="EXISTING")


def test_workspace_gate_wait_is_cancelable_and_external_restore_wakes(tmp_path):
    async def scenario():
        root = tmp_path / "missing"
        gate = WorkspaceExecutionGate(root)
        waiting = asyncio.create_task(gate.wait_available())
        await asyncio.sleep(0)
        assert not waiting.done()
        root.mkdir()
        gate.wake()
        await asyncio.wait_for(waiting, 1)
        root.rmdir()
        canceled = asyncio.create_task(gate.wait_available())
        await asyncio.sleep(0)
        canceled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await canceled
        closing = asyncio.create_task(gate.wait_available())
        await asyncio.sleep(0)
        gate.begin_close()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(closing), 1)

    asyncio.run(scenario())


def test_workspace_feedback_closed_codec_has_no_process_facts(tmp_path):
    fact = WorkspaceRecreatedFeedbackContent(
        "session:one", str(tmp_path), datetime.now(timezone.utc), "turn:one"
    )
    value = fact.canonical_mapping()
    assert value["kind"] == "workspace_recreated"
    assert not {"process", "process_id", "other_work_stopped"} & value.keys()
    lowered = project_user_control_feedback_for_provider(fact.canonical_bytes())
    assert "Original files were not recovered" in lowered
    assert str(tmp_path) in lowered


@pytest.mark.postgres
def test_restore_writer_intent_does_not_steal_effective_other_owner(
    stage2_migrated_postgres_database, tmp_path
):
    repository = ConversationKernelRepository(
        verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    )
    workspace = resolve_workspace(
        HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path)
    )
    arguments = dict(
        session_id=f"session:{uuid4().hex}",
        workspace_id=workspace.workspace_key,
        workspace_root=str(workspace.workspace_root),
        workspace_label=workspace.display_label,
        writer_owner_id="host:original",
        lease_seconds=60,
        deadline_monotonic=monotonic() + 30,
    )
    first = repository.acquire_host_writer(intent="NEW", **arguments)
    with pytest.raises(SessionWriterConflict):
        repository.acquire_host_writer(
            intent="RESTORE", **{**arguments, "writer_owner_id": "host:restore"}
        )
    repository.validate_host_writer(first.guard, deadline_monotonic=monotonic() + 30)
    renewed = repository.acquire_host_writer(intent="RESTORE", **arguments)
    assert renewed.guard == first.guard
    repository.release_host_writer(first.guard, deadline_monotonic=monotonic() + 30)
    restored = repository.acquire_host_writer(
        intent="RESTORE", **{**arguments, "writer_owner_id": "host:restore"}
    )
    assert restored.guard.writer_generation == first.guard.writer_generation + 1
    repository.release_host_writer(restored.guard, deadline_monotonic=monotonic() + 30)


@pytest.mark.postgres
@pytest.mark.parametrize("kind", ["project", "transient"])
def test_cold_history_and_explicit_restore_use_real_core_without_read_activation(
    kind,
    tmp_path,
    monkeypatch,
    stage2_migrated_postgres_database,
):
    from pulsara_agent.conversation_kernel import host as host_module

    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(host_module, "DirectKernelModelPort", _DogfoodModelPort)
    captures = []
    monkeypatch.setattr(
        host_module.LocalMcpManagementService,
        "load_configs",
        lambda *a, **kw: captures.append(kw) or (),
    )
    runtime = test_model_runtime(
        postgres_dsn=stage2_migrated_postgres_database.runtime_dsn
    )

    async def scenario():
        root = tmp_path / "workspace"
        root.mkdir()
        core = KernelHostCore.production(model_runtime=runtime)
        source = HostWorkspaceInput(workspace_kind=kind, workspace_root=root)
        first = await core.open_session(source)
        await first.update_model_call_binding(test_model_binding(runtime))
        result = await first.run_turn(
            PromptContent.text("Persist this original history.")
        )
        session_id = first.session_id
        initial_guard = first._lease.guard
        await core.close_session(first.host_session_id)
        shutil.rmtree(root)
        sessions = LocalSessionController(
            core=core,
            permission_policy=default_permission_policy(),
            active_skill_names=frozenset(),
        )
        bridge = LocalBrowserBridge(sessions=sessions, protocol_server=object())
        try:
            before = len(captures)
            assert (await core.list_resumable_sessions(workspace_input=source))[
                0
            ].session_id == session_id
            with pytest.raises(WorkspaceUnavailable):
                await core.resume_most_recent_session(source)
            assert not root.exists()
            await sessions.mcp_authorization(
                "unconfigured", action="status", session_id=session_id
            )
            assert len(captures) == before and sessions._by_session == {}
            with pytest.raises(WorkspaceUnavailable):
                await sessions.resume_session(session_id)
            cold = await bridge.connect(session_id, browser_instance_id="browser:cold")
            assert cold["view"] == "history"
            assert "connection_id" not in cold and "role" not in cold
            assert sessions._by_session == {} and bridge._connections == {}
            assert len(captures) == before and not root.exists()
            snapshot = await sessions.history_snapshot(session_id)
            assert (
                snapshot.snapshot.snapshot.control.latest_root_turn.status
                == "COMPLETED"
            )
            assert snapshot.snapshot.snapshot.entries[-1].entry_kind
            summary = await sessions.read_session(session_id)
            assert summary["latest_root_turn"]["status"] == "COMPLETED"
            for entry in snapshot.snapshot.snapshot.entries:
                reference = await sessions._history_reader(session_id)
                assert (
                    reference.resolve_content_reference(
                        session_id=session_id,
                        entry_id=entry.entry_id,
                        deadline_monotonic=monotonic() + 30,
                    )["session_id"]
                    == session_id
                )
            foreign = LocalSessionController(
                core=core,
                memory_domain_id="unrelated",
                permission_policy=default_permission_policy(),
                active_skill_names=frozenset(),
            )
            with pytest.raises(KeyError):
                await foreign.history_snapshot(session_id)
            child_id = f"session:{uuid4().hex}"
            with pytest.raises(WorkspaceUnavailable):
                await core.fork_conversation(
                    source_session_id=session_id,
                    anchor_entry_id=result.final_entry_id,
                    child_session_id=child_id,
                    memory_domain_id="u_local",
                )
            assert (
                await core.read_resumable_session(child_id, memory_domain_id="u_local")
                is None
            )
            one, two = await asyncio.gather(
                sessions.restore_missing_workspace(session_id),
                sessions.restore_missing_workspace(session_id),
            )
            assert one is two
            assert (
                one.session._lease.guard.writer_generation
                == initial_guard.writer_generation + 1
            )
            assert one.session._workspace_recreated_at is not None
            assert root.is_dir() and list(root.iterdir()) == []
            assert one.session.workspace.workspace_key == first.workspace.workspace_key
            assert (
                one.session.host_session_id == one.session._lease.guard.writer_owner_id
            )
            assert one.session._io is not first._io
        finally:
            await bridge.aclose()
            await sessions.aclose()
            await core.shutdown()

    asyncio.run(scenario())


@pytest.mark.postgres
def test_running_response_settles_but_accepted_queue_waits_for_directory(
    tmp_path,
    monkeypatch,
    stage2_migrated_postgres_database,
):
    from pulsara_agent.conversation_kernel import host as host_module

    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        host_module.LocalMcpManagementService, "load_configs", lambda *a, **kw: ()
    )
    runtime = test_model_runtime(
        postgres_dsn=stage2_migrated_postgres_database.runtime_dsn
    )

    async def scenario():
        root = tmp_path / "workspace"
        root.mkdir()
        model = _SteerModelPort()
        monkeypatch.setattr(host_module, "DirectKernelModelPort", lambda **kw: model)
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(
            HostWorkspaceInput(workspace_kind="project", workspace_root=root)
        )
        try:
            await session.update_model_call_binding(test_model_binding(runtime))
            running = asyncio.create_task(session.run_turn(PromptContent.text("First")))
            await asyncio.wait_for(model.started.wait(), 5)
            queued = await session.submit_prompt(
                command_id="command:parked", content=PromptContent.text("Next")
            )
            assert queued.status == "PENDING"
            shutil.rmtree(root)
            with pytest.raises(WorkspaceUnavailable):
                await session.submit_prompt(
                    command_id="command:rejected", content=PromptContent.text("Reject")
                )
            model.release.set()
            assert (await asyncio.wait_for(running, 5)).final_text == "BEFORE_STEER"
            await asyncio.sleep(0)
            assert len(model.requests) == 1
            assert (await session.query_command("command:parked")).status == "PENDING"
            guard = session._lease.guard
            await session.restore_missing_workspace()
            assert session._lease.guard == guard
            deadline = monotonic() + 5
            while (await session.query_command("command:parked")).status == "PENDING":
                assert monotonic() < deadline
                await asyncio.sleep(0.01)
            assert len(model.requests) == 2
            # Frozen prospective first request remains untouched; advisory may wait
            # for a later ordinary barrier, exactly as the specification allows.
            assert session._workspace_recreated_at is not None
        finally:
            await core.close_session(session.host_session_id)
            await core.shutdown()

    asyncio.run(scenario())


@pytest.mark.postgres
def test_unlaunched_tool_rejected_and_workspace_feedback_appends_at_ordinary_barrier(
    tmp_path,
    monkeypatch,
    stage2_migrated_postgres_database,
):
    from pulsara_agent.conversation_kernel import host as host_module
    from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader
    from tests.test_round2_terminal_host import _tool_call, _text
    from pulsara_agent.llm.input import MessageRole

    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        host_module.LocalMcpManagementService, "load_configs", lambda *a, **kw: ()
    )
    runtime = test_model_runtime(
        postgres_dsn=stage2_migrated_postgres_database.runtime_dsn
    )

    class MissingToolModel(_DogfoodModelPort):
        def __init__(self):
            self.started, self.release = asyncio.Event(), asyncio.Event()
            super().__init__()

        async def _stream(self, request):
            if len(self._delegate.requests) == 1:
                self.started.set()
                await self.release.wait()
                payloads = _tool_call(
                    "terminal",
                    "call:never-launch",
                    {
                        "command": "touch SHOULD_NOT_EXIST",
                        "yield_time_ms": 0,
                        "max_output_chars": 1000,
                    },
                )
            else:
                payloads = _text("text:restored", "RESTORED_OK")
            for payload in payloads:
                yield payload

    async def scenario():
        root = tmp_path / "workspace"
        root.mkdir()
        model = MissingToolModel()
        monkeypatch.setattr(host_module, "DirectKernelModelPort", lambda **kw: model)
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(
            HostWorkspaceInput(workspace_kind="project", workspace_root=root)
        )
        try:
            await session.update_model_call_binding(test_model_binding(runtime))
            run = asyncio.create_task(
                session.run_turn(PromptContent.text("Use the terminal once."))
            )
            await asyncio.wait_for(model.started.wait(), 5)
            shutil.rmtree(root)
            model.release.set()
            reader = CanonicalProtocolReader(session.repository.connection_provider)
            deadline = monotonic() + 5
            while True:
                snapshot = await asyncio.to_thread(
                    reader.snapshot,
                    session_id=session.session_id,
                    maximum_entries=256,
                    maximum_control_items=128,
                    deadline_monotonic=monotonic() + 5,
                )
                if any(
                    entry.entry_kind == wire.TOOL_RESULT for entry in snapshot.entries
                ):
                    break
                assert monotonic() < deadline
                await asyncio.sleep(0.01)
            assert (
                len(model._delegate.requests) == 1
                and not run.done()
                and not root.exists()
            )
            await session.restore_missing_workspace()
            result = await asyncio.wait_for(run, 5)
            assert result.final_text == "RESTORED_OK"
            assert not (root / "SHOULD_NOT_EXIST").exists()
            first, second = [
                request.compiled_input for request in model._delegate.requests
            ]
            assert (
                first.system_prompt == second.system_prompt
                and first.tools == second.tools
            )
            assert second.messages[: len(first.messages)] == first.messages
            tool_result = next(
                message
                for message in second.messages
                if message.role is MessageRole.TOOL_RESULT
            )
            assert "TOOL_UNAVAILABLE" in str(tool_result.content)
            assert "workspace_recreated" in str(second.messages)
            assert session._workspace_recreated_at is None
        finally:
            await core.close_session(session.host_session_id)
            await core.shutdown()

    asyncio.run(scenario())


@pytest.mark.postgres
def test_missing_directory_host_close_settles_root_and_child_with_original_close_reason(
    tmp_path,
    monkeypatch,
    stage2_migrated_postgres_database,
):
    from pulsara_agent.conversation_kernel import host as host_module
    from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader
    from tests.test_round2_terminal_host import _tool_call

    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        host_module.LocalMcpManagementService, "load_configs", lambda *a, **kw: ()
    )
    runtime = test_model_runtime(
        model_id="test-pro",
        wire_api="openai_chat_completions",
        postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
    )

    from tests.support.round3 import CallbackScriptedKernelModel

    class ClosingModel(CallbackScriptedKernelModel):
        def __init__(self):
            self.root_ready, self.child_ready, self.release = (
                asyncio.Event(),
                asyncio.Event(),
                asyncio.Event(),
            )
            self.root_calls, self.child_calls = 0, 0
            super().__init__(self._stream)

        async def _stream(self, request):
            if (
                request.compiled_input.canonical_input_identity.scope_subagent_task_id
                is None
            ):
                self.root_calls += 1
                if self.root_calls == 1:
                    payloads = _tool_call(
                        "spawn_agent",
                        "call:spawn",
                        {"task": "Run terminal once.", "task_name": "worker"},
                    )
                else:
                    self.root_ready.set()
                    await self.release.wait()
                    payloads = _tool_call(
                        "terminal",
                        "call:root-terminal",
                        {"command": "touch ROOT_NOT_STARTED", "yield_time_ms": 0},
                    )
            else:
                self.child_calls += 1
                self.child_ready.set()
                await self.release.wait()
                payloads = _tool_call(
                    "terminal",
                    "call:child-terminal",
                    {"command": "touch CHILD_NOT_STARTED", "yield_time_ms": 0},
                )
            for payload in payloads:
                yield payload

    async def scenario():
        root = tmp_path / "workspace"
        root.mkdir()
        model = ClosingModel()
        monkeypatch.setattr(host_module, "DirectKernelModelPort", lambda **kw: model)
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(
            HostWorkspaceInput(workspace_kind="project", workspace_root=root)
        )
        run = None
        try:
            await session.update_model_call_binding(test_model_binding(runtime))
            run = asyncio.create_task(
                session.run_turn(PromptContent.text("Spawn worker and wait."))
            )
            try:
                await asyncio.wait_for(
                    asyncio.gather(model.root_ready.wait(), model.child_ready.wait()),
                    10,
                )
            except TimeoutError:
                raise AssertionError(
                    str([request.compiled_input.messages for request in model.requests])
                )
            root_turn = session._active_turn_id
            child = next(iter(session._subagents._tasks.values()))
            shutil.rmtree(root)
            model.release.set()
            reader = CanonicalProtocolReader(session.repository.connection_provider)
            deadline = monotonic() + 10
            while True:
                snapshot = await asyncio.to_thread(
                    reader.snapshot,
                    session_id=session.session_id,
                    maximum_entries=256,
                    maximum_control_items=128,
                    deadline_monotonic=monotonic() + 5,
                )
                if (
                    sum(
                        entry.entry_kind == wire.TOOL_RESULT
                        for entry in snapshot.entries
                    )
                    >= 3
                ):
                    break
                assert monotonic() < deadline
                await asyncio.sleep(0.01)
            assert (model.root_calls, model.child_calls) == (2, 1)
            await asyncio.wait_for(core.close_session(session.host_session_id), 10)
            for turn_id in (root_turn, child.cancellation_intent.turn_id):
                outcome = session.repository.read_turn_terminal_outcome(
                    session_id=session.session_id,
                    turn_id=turn_id,
                    deadline_monotonic=monotonic() + 5,
                )
                assert (
                    outcome["status"] == "INTERRUPTED"
                    and outcome["terminal_reason"] == "SESSION_CLOSED"
                )
            task = session.repository.query_subagent_task(
                session_id=session.session_id,
                task_id=child.task_id,
                deadline_monotonic=monotonic() + 5,
            )
            assert (
                task["status"] == "INTERRUPTED"
                and task["terminal_reason"] == "HOST_CLOSING"
            )
            assert not root.exists()
        finally:
            model.release.set()
            await core.shutdown()
            if run is not None:
                await asyncio.gather(run, return_exceptions=True)

    asyncio.run(scenario())


def test_browser_write_gate_rejects_takeover_while_directory_read_is_pending(tmp_path):
    from types import SimpleNamespace
    from pulsara_agent.web_app.protocol_client import ProtocolBridgeError

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def availability(session_id):
            entered.set()
            await release.wait()
            return {"path": str(tmp_path), "outcome": "AVAILABLE"}

        sessions = SimpleNamespace(
            workspace_availability=availability, _lock=asyncio.Lock(), _operations={}
        )
        bridge = LocalBrowserBridge(sessions=sessions, protocol_server=object())
        a = SimpleNamespace(
            session_id="one", role="controller", generation=1, is_open=True
        )
        bridge._connections["a"] = a
        bridge._controller_by_session["one"] = "a"
        pending = asyncio.create_task(
            bridge.require_chat_controller("a", 1, session_id="one")
        )
        await entered.wait()
        bridge._controller_by_session["one"] = "b"
        release.set()
        with pytest.raises(ProtocolBridgeError):
            await pending

    asyncio.run(scenario())


@pytest.mark.postgres
def test_host_close_joins_prepared_queue_admission_blocked_by_directory(
    tmp_path,
    monkeypatch,
    stage2_migrated_postgres_database,
):
    from pulsara_agent.conversation_kernel import host as host_module
    from pulsara_agent.conversation_kernel.contracts import PromptDeliveryMode

    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        host_module.LocalMcpManagementService, "load_configs", lambda *a, **kw: ()
    )
    runtime = test_model_runtime(
        postgres_dsn=stage2_migrated_postgres_database.runtime_dsn
    )

    async def scenario():
        root = tmp_path / "workspace"
        root.mkdir()
        model = _SteerModelPort()
        monkeypatch.setattr(host_module, "DirectKernelModelPort", lambda **kw: model)
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(
            HostWorkspaceInput(workspace_kind="project", workspace_root=root)
        )
        try:
            await session.update_model_call_binding(test_model_binding(runtime))
            first = asyncio.create_task(session.run_turn(PromptContent.text("First")))
            await asyncio.wait_for(model.started.wait(), 5)
            queued = await session.submit_prompt(
                command_id="command:prepared-close", content=PromptContent.text("Next")
            )
            assert queued.status == "PENDING"
            prepared, release, blocked = (
                asyncio.Event(),
                asyncio.Event(),
                asyncio.Event(),
            )
            prepare = session._runner.prepare_prospective_root_input
            wait = session.workspace_gate.wait_available

            async def pause(*args, **kwargs):
                dispatch = await prepare(*args, **kwargs)
                prepared.set()
                await release.wait()
                return dispatch

            async def observe_wait():
                if not root.exists():
                    blocked.set()
                await wait()

            monkeypatch.setattr(
                session._runner, "prepare_prospective_root_input", pause
            )
            monkeypatch.setattr(session.workspace_gate, "wait_available", observe_wait)
            model.release.set()
            await asyncio.wait_for(first, 5)
            await asyncio.wait_for(prepared.wait(), 5)
            shutil.rmtree(root)
            release.set()
            await asyncio.wait_for(blocked.wait(), 5)
            await asyncio.wait_for(core.close_session(session.host_session_id), 10)
            assert len(model.requests) == 1 and not root.exists()
            assert (
                session.repository.pending_prompt_head_mode(
                    session_id=session.session_id, deadline_monotonic=monotonic() + 5
                )
                is PromptDeliveryMode.NEW_TURN
            )
        finally:
            model.release.set()
            await core.shutdown()

    asyncio.run(scenario())
