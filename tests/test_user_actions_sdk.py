"""User-action observations through the production Host and official SDK HTTP."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import shutil
from threading import Event as ThreadEvent
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.execution_watchdogs import KernelWatchdogOwner
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import LLMMessage, PromptContent
from pulsara_agent.model_input.contracts import ContextSourceKind
from pulsara_agent.model_input.compiler import StructuredModelInputCompiler
from pulsara_agent.model_input.continuity import decode_runtime_observation
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.terminal_process import TerminalProcessOrigin, TerminalRequest
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.support.model_config import (
    test_model_binding as model_binding,
    test_model_runtime as model_runtime,
)
from tests.test_http413_compaction_sdk import (
    APIS,
    ByteGate,
    _messages,
    _rows,
    byte_gated_server,
)


def _user_actions(request):
    observations = []
    for role, text in _messages(request):
        if role != "user" or not text.startswith('{"pulsara_runtime_observation":'):
            continue
        observation = decode_runtime_observation(LLMMessage.user(text))
        if observation.source_kind is ContextSourceKind.USER_ACTIONS:
            observations.append(observation)
    return observations


def _canonical_rows(session):
    entries = _rows(session,
        "SELECT id,entry_kind,inline_content FROM pulsara_v3.transcript_entries "
        "WHERE session_id=%s ORDER BY entry_sequence", (session.session_id,))
    events = _rows(session,
        "SELECT event_id,event_type,payload FROM pulsara_v3.agent_events "
        "WHERE session_id=%s ORDER BY event_sequence", (session.session_id,))
    return entries, events


def _wire_value(request, name):
    """Retain the SDK's exact JSON bytes for a known request member."""
    text = request.raw.decode()
    start = text.index(json.dumps(name) + ":") + len(json.dumps(name)) + 1
    _, length = json.JSONDecoder().raw_decode(text[start:])
    return text[start:start + length].encode()


@pytest.mark.postgres
@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize(("installed_epoch", "omit_action"), (
    (False, False), (True, False), (True, True),
))
def test_workspace_user_action_is_one_shot_sdk_suffix_without_canonical_write(
    tmp_path, monkeypatch, isolated_migrated_postgres_database, api, installed_epoch, omit_action,
):
    """Cold and installed compilers preserve the same advisory action contract."""
    import pulsara_agent.conversation_kernel.host as kernel_host

    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setattr(kernel_host.LocalMcpManagementService, "load_configs", lambda *_a, **_k: ())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "original.txt").write_text("This file must not be restored.")
    gate = ByteGate()

    async def run():
        with byte_gated_server(api, gate) as endpoint:
            runtime = model_runtime(base_url=endpoint, wire_api=api,
                postgres_dsn=isolated_migrated_postgres_database.runtime_dsn)
            runtime.settings.value = replace(runtime.settings.read(), input_suggestions_enabled=False)
            core = KernelHostCore.production(model_runtime=runtime)
            session = await core.open_session(HostWorkspaceInput(
                workspace_kind="project", workspace_root=workspace))
            try:
                await session.update_model_call_binding(model_binding(runtime))
                session.repository.rename_session(session_id=session.session_id,
                    memory_domain_id=session._memory_domain_id, title="SDK action acceptance",
                    deadline_monotonic=monotonic() + 30)

                async def normal_turn(text):
                    result = await session.run_turn(PromptContent.text(text),
                        command_id=random_id("command"),
                        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
                    assert result.final_text == "HTTP413_FINISHED"
                    return result

                previous = await normal_turn("Warm up the existing conversation.") if installed_epoch else None
                before = _canonical_rows(session)
                request_count = len(gate.requests)
                shutil.rmtree(workspace)
                await session.restore_missing_workspace()
                assert workspace.is_dir()
                assert list(workspace.iterdir()) == []
                assert _canonical_rows(session) == before
                assert len(gate.requests) == request_count

                preview = await session.read_context_usage()
                assert preview["state"] == ("ready" if installed_epoch else "empty")
                if previous is not None:
                    # This is the real semantic-only source path used by an
                    # auxiliary summary, before any normal business prepare.
                    dispatch = session._runner._provider_dispatch
                    source = await dispatch.prepare_compaction_source(
                        turn_id=previous.turn_id, model_call_index=1,
                        inherited_memory_use_policy=session._runner._root_memory_use_policy,
                        deadline=monotonic() + 30, allow_terminal_compaction=True,
                        independent_auxiliary_source=True,
                    )
                    try:
                        assert ContextSourceKind.USER_ACTIONS not in {
                            item.source_kind for item in source.sources.candidates
                        }
                    finally:
                        source.close()
                assert _canonical_rows(session) == before
                assert len(gate.requests) == request_count

                omitted_batches = []
                if omit_action:
                    compiler = session._runner._provider_dispatch._compiler
                    original_compile = compiler.compile_installed_append

                    def compile_at_aggregate_boundary(request, **kwargs):
                        actions = [item for item in request.sources.candidates
                            if item.source_kind is ContextSourceKind.USER_ACTIONS]
                        if not actions:
                            return original_compile(request, **kwargs)
                        # Use the actual frozen production sources to place
                        # the physical source budget exactly at the business
                        # baseline. The optional complete batch must be dropped.
                        baseline_bytes = sum(item.variants[0].utf8_bytes
                            for item in request.sources.candidates
                            if item.source_kind is not ContextSourceKind.USER_ACTIONS)
                        omitted_batches.append(actions[0])
                        constrained = StructuredModelInputCompiler(limits=replace(
                            compiler._limits, maximum_aggregate_full_source_bytes=baseline_bytes))
                        return constrained.compile_installed_append(request, **kwargs)

                    monkeypatch.setattr(compiler, "compile_installed_append", compile_at_aggregate_boundary)

                await normal_turn("Continue with the current request.")
                action_request = gate.requests[-1]
                observations = _user_actions(action_request)
                if omit_action:
                    assert omitted_batches
                    assert observations == []
                else:
                    assert len(observations) == 1
                    observation = observations[0]
                    assert observation.presence.value == "VALUE"
                    actions = json.loads(observation.body)["actions"]
                    assert len(actions) == 1
                    action = actions[0]
                    assert action["kind"] == "workspace_recreated"
                    assert Path(action["workspace_root"]) == workspace
                    assert action["original_files_restored"] is False
                    assert action["created_at"]

                # Calling restore on an existing directory is a no-op and must
                # neither repeat the action nor append a CLEARED placeholder.
                after_action = _canonical_rows(session)
                await session.restore_missing_workspace()
                assert _canonical_rows(session) == after_action
                await normal_turn("Answer this next independent request.")
                assert _user_actions(gate.requests[-1]) == observations
                assert len(gate.requests) == request_count + 2
                entries, events = _canonical_rows(session)
                assert {row["entry_kind"] for row in entries} == {"USER_MESSAGE", "ASSISTANT_MESSAGE"}
                assert len(entries) == 2 * len(gate.requests)
                assert "workspace_recreated" not in b"".join(bytes(row["inline_content"]) for row in entries).decode()
                assert all("Control" not in row["event_type"] for row in events)
                assert "workspace_recreated" not in json.dumps([row["payload"] for row in events])
            finally:
                await core.shutdown()

    asyncio.run(run())
    expected_path = "/v1/chat/completions" if api == APIS[0] else "/v1/responses"
    assert {request.path for request in gate.requests} == {expected_path}
    for previous, current in zip(gate.requests, gate.requests[1:]):
        message_key = "messages" if api == APIS[0] else "input"
        previous_messages = _wire_value(previous, message_key)
        current_messages = _wire_value(current, message_key)
        assert current_messages.startswith(previous_messages[:-1] + b",")
        if "instructions" in previous.document:
            assert _wire_value(current, "instructions") == _wire_value(previous, "instructions")
        assert _wire_value(current, "tools") == _wire_value(previous, "tools")
    assert all("pulsara_user_control_feedback" not in request.raw.decode() for request in gate.requests)


@pytest.mark.postgres
@pytest.mark.parametrize("api", APIS)
def test_real_process_stop_after_final_notifies_only_next_business_request(
    tmp_path, monkeypatch, isolated_migrated_postgres_database, api,
):
    """A physically pending user control cannot keep an assistant final open."""
    import pulsara_agent.conversation_kernel.host as kernel_host

    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setattr(kernel_host.LocalMcpManagementService, "load_configs", lambda *_a, **_k: ())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gate = ByteGate()

    async def run():
        with byte_gated_server(api, gate) as endpoint:
            runtime = model_runtime(base_url=endpoint, wire_api=api,
                postgres_dsn=isolated_migrated_postgres_database.runtime_dsn)
            runtime.settings.value = replace(runtime.settings.read(), input_suggestions_enabled=False)
            core = KernelHostCore.production(model_runtime=runtime)
            session = await core.open_session(HostWorkspaceInput(
                workspace_kind="project", workspace_root=workspace))
            terminal = session._tools._terminal
            owner = session._tools._terminal_owner_id
            model_collected = asyncio.Event()
            release_model = asyncio.Event()
            physical_started = asyncio.Event()
            release_physical = ThreadEvent()
            active = None
            attempt = None
            process_id = None
            physical_calls = []
            try:
                await session.update_model_call_binding(model_binding(runtime))
                session.repository.rename_session(session_id=session.session_id,
                    memory_domain_id=session._memory_domain_id, title="SDK late physical control",
                    deadline_monotonic=monotonic() + 30)

                async def normal_turn(text):
                    result = await session.run_turn(PromptContent.text(text),
                        command_id=random_id("command"),
                        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
                    assert result.final_text == "HTTP413_FINISHED"
                    assert result.model_call_count == 1
                    assert result.tool_call_count == 0
                    return result

                warmup = await normal_turn("Establish this conversation.")
                # Fixture setup uses the real Host-owned manager. The model
                # never receives or claims a tool call for this process start.
                started = await asyncio.to_thread(terminal.execute,
                    TerminalRequest("sleep 600", yield_time_ms=0),
                    owner_host_session_id=owner,
                    decision_deadline_monotonic=session._tools._deadlines.deadline(
                        KernelWatchdogOwner.TERMINAL_FOREGROUND_DECISION),
                    origin=TerminalProcessOrigin(warmup.turn_id, "ROOT", session.session_id))
                assert started.status.value == "running"
                process_id = started.process_id
                assert process_id is not None
                assert [item.process_id for item in session._tools.list_background_terminal_processes()] == [process_id]

                original_collect = session._runner._collect_model

                async def collect_before_final(*args, **kwargs):
                    collected = await original_collect(*args, **kwargs)
                    model_collected.set()
                    await release_model.wait()
                    return collected

                original_terminate = session._tools.terminate_background_terminal_process
                loop = asyncio.get_running_loop()

                def terminate_after_final(target, **kwargs):
                    physical_calls.append(target)
                    loop.call_soon_threadsafe(physical_started.set)
                    # The test's finally block always releases this owner.
                    # It still performs the original real physical operation.
                    release_physical.wait()
                    return original_terminate(target, **kwargs)

                monkeypatch.setattr(session._runner, "_collect_model", collect_before_final)
                monkeypatch.setattr(session._tools, "terminate_background_terminal_process", terminate_after_final)
                active = asyncio.create_task(normal_turn("Finish the current request now."))
                await asyncio.wait_for(model_collected.wait(), timeout=30)
                assert len(gate.requests) == 2
                assert all(_user_actions(request) == [] for request in gate.requests)

                accepted = await session.request_terminate_background_process(
                    command_id=random_id("command"),
                    process_id=process_id,
                    expected_session_id=session.session_id,
                    expected_host_session_id=session.host_session_id,
                    admission_deadline_ms=session.control_admission_deadline_ms())
                assert accepted.status == "PENDING"
                assert accepted.user_control.accepted is True
                attempt = session._user_control_attempts[accepted.command_id]
                await asyncio.wait_for(physical_started.wait(), timeout=30)
                assert not attempt.task.done()
                assert terminal.get_process_info(process_id,
                    owner_host_session_id=owner).status == "running"

                release_model.set()
                final = await asyncio.wait_for(asyncio.shield(active), timeout=10)
                assert final.final_text == "HTTP413_FINISHED"
                assert not release_physical.is_set()
                assert not attempt.task.done()
                query = await session.query_control_command(attempt.request)
                assert query.outcome.status == "PENDING"
                assert len(gate.requests) == 2
                before_physical_result = _canonical_rows(session)

                release_physical.set()
                await asyncio.wait_for(asyncio.shield(attempt.task), timeout=30)
                query = await session.query_control_command(attempt.request)
                assert query.outcome.status == "SUCCEEDED"
                process = query.outcome.user_control.process
                assert process.disposition == "TERMINATION_COMPLETED"
                assert process.status == "killed"
                assert process.group_alive is False
                assert process.physical_state == "PHYSICALLY_JOINED"
                info = terminal.get_process_info(process_id, owner_host_session_id=owner)
                assert info.status == "killed"
                assert info.exit_code == process.exit_code
                assert physical_calls == [process_id]
                assert session.active_root_turn_id() is None
                assert len(gate.requests) == 2
                assert _canonical_rows(session) == before_physical_result

                await normal_turn("Report the current situation briefly.")
                assert len(gate.requests) == 3
                observations = _user_actions(gate.requests[-1])
                assert len(observations) == 1
                actions = json.loads(observations[0].body)["actions"]
                assert len(actions) == 1
                action = actions[0]
                assert action["kind"] == "process_control"
                assert action["target"] == {"kind": "PROCESS", "process_id": process_id}
                assert action["origin"] == "ROOT"
                for key in ("disposition", "status", "exit_code", "physical_state", "group_alive"):
                    assert action[key] == getattr(process, key)
                assert physical_calls == [process_id]
            finally:
                release_model.set()
                release_physical.set()
                try:
                    tasks = [task for task in (active, attempt.task if attempt is not None else None)
                        if task is not None]
                    if tasks:
                        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=30)
                finally:
                    try:
                        if process_id is not None:
                            await asyncio.to_thread(terminal.terminate_process_if_running,
                                process_id, owner_host_session_id=owner)
                    finally:
                        await core.shutdown()

    asyncio.run(run())
