"""Hook stdin identity is built by production lifecycle and tool owners."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from time import monotonic
from types import SimpleNamespace

import pytest

from pulsara_agent.hooks.contracts import (
    HookDispatchScopeRef, HookEventType, HookScopeKind, HookScopePolicy,
    SessionStartInput,
)
from pulsara_agent.hooks.context import HookContextOwner
from pulsara_agent.hooks.dispatcher import KernelHookDispatcher
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from tests.test_round9_2_hook_subsystem import _definition, _view, _ScriptedExecutor


def _dispatcher(tmp_path, events):
    definitions = tuple(replace(_definition(tmp_path, event, event.external_name), scope_policy=HookScopePolicy.BOTH) for event in events)
    executor = _ScriptedExecutor({event.external_name: (0, b"", b"") for event in events})
    dispatcher = KernelHookDispatcher(initial_view=_view(tmp_path, definitions), workspace_root=tmp_path,
        credential_boundary=ProcessCredentialBoundary(), executor=executor)
    return dispatcher, executor


def test_public_input_requires_explicit_facts_and_envelope_rejects_false_session(tmp_path):
    from pulsara_agent.hooks.contracts import HookDispatchEnvelope, SessionStartRef
    with pytest.raises(TypeError):
        SessionStartInput(session_id="child", cwd=str(tmp_path), model="model", source="startup", permission_mode="default")
    public = SessionStartInput(session_id="child", cwd=str(tmp_path), model="model", source="startup",
                              permission_mode="default", session_kind="ROOT", input_origin=None)
    with pytest.raises(ValueError, match="actual session scope"):
        HookDispatchEnvelope(_view(tmp_path, ()), HookDispatchScopeRef(object(), object(), HookScopeKind.SUBAGENT, "child"),
                             public, SessionStartRef(object(), "startup"), monotonic()+5)


def test_child_session_start_and_compaction_owners_send_actual_identity(tmp_path):
    from pulsara_agent.conversation_kernel.runner import ConversationKernelRunner, _SessionStartColdBoundaryOwner, _RootSessionStartCompactPort
    from pulsara_agent.conversation_kernel.cancellation import ActiveTurnCancellationIntent
    from pulsara_agent.conversation_kernel.compaction.coordinator import CompactionCoordinator, PreparedCompactSessionStartFacts, _new_compaction_attempt_token
    from pulsara_agent.conversation_kernel.compaction.contracts import CompactionScope, CompactionTrigger
    from pulsara_agent.primitives.permission import PermissionMode

    async def scenario():
        dispatcher, executor = _dispatcher(tmp_path, (HookEventType.SESSION_START_EVENT, HookEventType.SUBAGENT_START_EVENT, HookEventType.PRE_COMPACT_EVENT, HookEventType.POST_COMPACT_EVENT))
        session = "session:actual-child"
        scope = HookDispatchScopeRef(object(), object(), HookScopeKind.SUBAGENT, session)
        context = HookContextOwner()
        context.register_scope(scope)
        permission = SimpleNamespace(snapshot_id="permission:actual", effective_mode=PermissionMode.BYPASS_PERMISSIONS, plan_workflow_id=None)
        lease = SimpleNamespace(guard=SimpleNamespace(session_id=session))
        tools = SimpleNamespace(snapshot_workspace_root=lambda: tmp_path)
        try:
            runner = object.__new__(ConversationKernelRunner)
            runner._session_start_boundary = _SessionStartColdBoundaryOwner("resume")
            runner._session_scope_kind = ModelInputScopeKind.SUBAGENT
            runner._hook_dispatcher, runner._hook_context_owner, runner._hook_scope = dispatcher, context, scope
            runner._writer_lease, runner._tools = lease, tools
            await runner._dispatch_initial_session_start(ActiveTurnCancellationIntent("turn:child", ModelInputScopeKind.SUBAGENT),
                permission_snapshot=permission, model_id="model:test", deadline_monotonic=monotonic()+5)
            attempt = _new_compaction_attempt_token(session_id=session, turn_id="turn:compact", scope_kind=ModelInputScopeKind.SUBAGENT, trigger=CompactionTrigger.MANUAL)
            boundary = _SessionStartColdBoundaryOwner("resume")
            await boundary.arm_compact_boundary(attempt_token=attempt, adopted_snapshot_id="snapshot:child", adopted_binding_revision_id="revision:child")
            facts = PreparedCompactSessionStartFacts(attempt_token=attempt, adopted_snapshot_id="snapshot:child", adopted_binding_revision_id="revision:child",
                scope=CompactionScope(session, "workspace:test", "turn:compact", ModelInputScopeKind.SUBAGENT), turn_id="turn:compact",
                model_id="model:test", permission_snapshot=permission, deadline_monotonic=monotonic()+5)
            compact_start = _RootSessionStartCompactPort(dispatcher, context, scope, boundary,
                ActiveTurnCancellationIntent("turn:compact", ModelInputScopeKind.SUBAGENT), session, str(tmp_path))
            assert (await compact_start(facts)).proceed
            coordinator = object.__new__(CompactionCoordinator)
            coordinator._hook_dispatcher, coordinator._writer_lease, coordinator._tools = dispatcher, lease, tools
            dispatch = SimpleNamespace(prepared_call=SimpleNamespace(call=SimpleNamespace(target=SimpleNamespace(fact=SimpleNamespace(model_id="model:test")))))
            assert (await coordinator._dispatch_pre_compact(dispatch=dispatch, scope=scope, attempt_token=attempt,
                turn_id="turn:compact", trigger=CompactionTrigger.MANUAL, deadline=monotonic()+5))[0]
            candidate = SimpleNamespace(snapshot=SimpleNamespace(snapshot_id="snapshot:child"), binding=SimpleNamespace(binding_revision_id="revision:child"))
            assert (await coordinator._dispatch_post_compact(model_id="model:test", cwd=str(tmp_path), scope=scope,
                attempt_token=attempt, candidate=candidate, turn_id="turn:compact", trigger=CompactionTrigger.MANUAL, deadline=monotonic()+5))[0]
            payloads = [item.public_stdin for item in executor.calls]
            assert [item["hook_event_name"] for item in payloads] == ["SessionStart", "SubagentStart", "SessionStart", "SubagentStart", "PreCompact", "PostCompact"]
            assert [item.get("source") for item in payloads[:4]] == ["resume", "resume", "compact", "compact"]
            assert all(item["session_id"] == session and item["session_kind"] == "SUBAGENT" and item["input_origin"] is None for item in payloads)
        finally:
            await dispatcher.aclose(deadline_monotonic=monotonic()+5)

    asyncio.run(scenario())


def test_code_tool_pre_and_post_owners_send_actual_program_source(tmp_path):
    from pulsara_agent.conversation_kernel.tool_execution import ToolBatchExecutor, CodeModeToolRequest, _code_snapshot
    from pulsara_agent.conversation_kernel.tool_contracts import PreparedResolvedToolInvocation
    from pulsara_agent.conversation_kernel.tool_invocations import ToolInvocationSource, ToolInvocationSourceKind, current_tool_invocation
    from pulsara_agent.primitives.context import freeze_json
    from pulsara_agent.primitives.permission import PermissionMode

    async def scenario():
        dispatcher, executor = _dispatcher(tmp_path, (HookEventType.PRE_TOOL_USE_EVENT, HookEventType.POST_TOOL_USE_EVENT))
        scope = HookDispatchScopeRef(object(), object(), HookScopeKind.ROOT, "session:root")
        owner = object.__new__(ToolBatchExecutor)
        owner._hooks, owner._hook_context = dispatcher, None
        owner._tools = SimpleNamespace(snapshot_workspace_root=lambda: tmp_path)
        owner._deadlines = SimpleNamespace(deadline=lambda _: monotonic()+5)
        request = CodeModeToolRequest("session:root", "model:test", None, None)
        canonical = SimpleNamespace(run_permission_snapshot=SimpleNamespace(effective_mode=PermissionMode.BYPASS_PERMISSIONS, plan_workflow_id=None))
        prepared = PreparedResolvedToolInvocation("read_file", "read_file", freeze_json({"path": "actual.txt"}))
        source = ToolInvocationSource("call:program", ToolInvocationSourceKind.CODE_MODE_CALL, "turn:source",
            source_exec_attempt_id="attempt:source", call_sequence=1, tool_name="read_file", original_arguments=prepared.resolved_arguments)
        source_token = current_tool_invocation.set(source)
        snapshot_token = _code_snapshot.set(SimpleNamespace(hook_epoch_nonce=object()))
        try:
            await owner._dispatch_pre_tool(view=dispatcher.capture_view(), scope=scope, prepared=prepared,
                turn_id="turn:source", assistant_entry_id="entry:source", tool_call_id="call:program", request=request, canonical_facts=canonical)
            settlement = SimpleNamespace(turn_id="turn:source", tool_call_id="call:program", result_id="result:actual", result_entry_id="entry:result",
                result_state="SUCCESS", public_arguments=prepared.resolved_arguments, public_projection=SimpleNamespace(canonical_body="actual result"))
            await owner._dispatch_post_tool(view=dispatcher.capture_view(), scope=scope, prepared=prepared,
                settlement=settlement, request=request, canonical_facts=canonical, continuity_scope=None)
            payloads = [item.public_stdin for item in executor.calls]
            assert [item["hook_event_name"] for item in payloads] == ["PreToolUse", "PostToolUse"]
            assert all(item["session_id"] == "session:root" and item["session_kind"] == "ROOT" and item["input_origin"] == "CODE_MODE_CALL" for item in payloads)
        finally:
            _code_snapshot.reset(snapshot_token)
            current_tool_invocation.reset(source_token)
            await dispatcher.aclose(deadline_monotonic=monotonic()+5)

    asyncio.run(scenario())


@pytest.mark.postgres
def test_child_final_real_host_dispatches_stop_with_actual_session(tmp_path, monkeypatch, stage2_migrated_postgres_database):
    import json
    import shlex
    import sys
    from pulsara_agent.identity import random_id
    from pulsara_agent.llm.input import PromptContent, text_part_values
    from pulsara_agent.primitives.permission import PermissionMode
    from tests.test_agent_session_host import AgentModel, eventually
    from tests.test_round4_plan_host import _open_test_session
    from tests.test_round9_2_hook_integration_postgres import _install_trusted_sources

    async def scenario():
        home = tmp_path / "home"
        log = tmp_path / "hook-inputs.jsonl"
        script = tmp_path / "capture.py"
        script.write_text("import json,sys\nfrom pathlib import Path\nv=json.load(sys.stdin)\nwith Path(sys.argv[1]).open('a') as f:f.write(json.dumps(v)+'\\n')\nif v['hook_event_name'] in ('SessionStart', 'SubagentStart'): print('START_CONTEXT_'+v['hook_event_name'])\n")
        command = " ".join(shlex.quote(str(item)) for item in (sys.executable, script, log))
        config = {"hooks": {event: [{"matcher": "*", "hooks": [{"type": "command", "command": command, "scope_policy": "BOTH"}]}]
                            for event in ("SessionStart", "SubagentStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SubagentStop", "SessionEnd")}}
        _install_trusted_sources(home=home, workspace_root=tmp_path, user_config=config, workspace_config=None)
        monkeypatch.setenv("PULSARA_HOME", str(home))
        model = AgentModel()
        model.first_child_release.set()
        core, root = await _open_test_session(tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        model.root_id = root.session_id
        try:
            await asyncio.wait_for(root.run_turn(PromptContent.text("Delegate one work"), command_id=random_id("command"),
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS), 15)

            async def completed():
                ids = await root._io.run(root.repository.list_root_agent_session_ids,
                    root_session_id=root.session_id, deadline_monotonic=monotonic()+5)
                if not ids:
                    return False
                model.agent_id = ids[0]
                works = await root._io.run(root.repository.list_agent_works, root_session_id=root.session_id,
                    agent_id=model.agent_id, limit=10, deadline_monotonic=monotonic()+5)
                return works and works[0]["status"] == "completed"

            await eventually(completed)
            await asyncio.wait_for(root.run_turn(PromptContent.text('Continue the same work'),
                command_id=random_id('command'), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS), 15)

            async def both_completed():
                works = await root._io.run(root.repository.list_agent_works, root_session_id=root.session_id,
                    agent_id=model.agent_id, limit=10, deadline_monotonic=monotonic()+5)
                return len(works) == 2 and all(work['status'] == 'completed' for work in works)

            await eventually(both_completed)
            works = await root._io.run(root.repository.list_agent_works, root_session_id=root.session_id,
                agent_id=model.agent_id, limit=10, deadline_monotonic=monotonic()+5)
        finally:
            await core.shutdown()
        assert len(model.child_requests) == 2
        before, after = (request.compiled_input for request in model.child_requests)
        rendered = "\n".join(text for message in before.messages for text in text_part_values(message.content))
        assert rendered.count("START_CONTEXT_SessionStart") == 1
        assert rendered.count("START_CONTEXT_SubagentStart") == 1
        assert rendered.index("START_CONTEXT_SessionStart") < rendered.index("START_CONTEXT_SubagentStart")
        assert after.system_prompt == before.system_prompt
        assert after.tools == before.tools
        assert after.messages[:len(before.messages)] == before.messages
        payloads = [json.loads(line) for line in log.read_text().splitlines()]
        child = [item for item in payloads if item["session_id"] == model.agent_id]
        assert {item["hook_event_name"] for item in child} >= {"SessionStart", "SubagentStart", "UserPromptSubmit", "Stop", "SubagentStop", "SessionEnd"}
        assert [item["hook_event_name"] for item in child if item["hook_event_name"] in {"SessionStart", "SubagentStart"}] == ["SessionStart", "SubagentStart"]
        assert not any(item["hook_event_name"] in {"SubagentStart", "SubagentStop"} and item["session_kind"] == "ROOT" for item in payloads)
        stops = [item for item in child if item["hook_event_name"] == "SubagentStop"]
        assert [item["turn_id"] for item in stops] == [work["turn_id"] for work in works]
        assert [item["stop_hook_active"] for item in stops] == [False, False]
        assert [item["last_assistant_message"] for item in stops] == ["CHILD_FIRST_FINAL", "CHILD_SECOND_FINAL"]
        assert all(item["session_kind"] == "SUBAGENT" for item in child)
        assert all(item["input_origin"] is None for item in child if item["hook_event_name"] != "UserPromptSubmit")
        submissions = [item for item in child if item['hook_event_name'] == 'UserPromptSubmit']
        assert [item['input_origin'] for item in submissions] == ['AGENT_INSTRUCTION', 'AGENT_INSTRUCTION']
        assert [item['prompt'] for item in submissions] == ['CHILD_FIRST', 'CHILD_SECOND']
        assert [item['turn_id'] for item in submissions] == [work['turn_id'] for work in works]
        assert all(item['permission_mode'] == 'bypassPermissions' and item['model'] for item in submissions)
        tools = [item for item in payloads if item["hook_event_name"] in {"PreToolUse", "PostToolUse"}]
        assert tools and all(item["input_origin"] == "MODEL_TOOL_CALL" for item in tools)

    asyncio.run(scenario())
