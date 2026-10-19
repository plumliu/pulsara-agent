"""Production gateway/Host/DB regressions for blocking interaction redirects."""
from __future__ import annotations

import asyncio
from pathlib import Path
from time import monotonic

import pytest
from psycopg.rows import dict_row

from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent, text_part_values
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from pulsara_agent.terminal_protocol.v3_gateway import TerminalKernelProtocolServer, _Connection
from tests.test_round4_plan_host import _DetachedDraftModel, _open_test_session, _text, _tool_call

pytestmark = pytest.mark.postgres


class _BlockingModel(_DetachedDraftModel):
    def __init__(self, root: Path, capability: bool, completed: bool = False):
        super().__init__()
        self.root = root
        self.capability = capability
        self.completed = completed
        self.queued_seen = asyncio.Event()

    async def stream(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            first = _tool_call('manage_capability', 'call:configuration', {
                'action': 'ADD_LOCAL_MCP', 'scope': 'USER', 'server_id': 'steer-fixture',
            }) if self.capability else _tool_call('write_file', 'call:first-write', {
                'path': str(self.root/'first.txt'), 'content': 'must not be written',
            })
            events = [*first, *_tool_call('write_file', 'call:second-write', {
                'path': str(self.root/'second.txt'), 'content': 'must not run after steer',
            })]
        elif len(self.requests) == 2:
            current = request.compiled_input
            original = self.requests[0].compiled_input
            assert current.system_prompt == original.system_prompt
            assert current.tools == original.tools
            rendered = '\n'.join(part for message in current.messages for part in text_part_values(message.content))
            assert rendered.count('被用户取消') == (1 if self.completed else 2)
            assert rendered.index('被用户取消') < rendered.index('STEER_KEEP_WORKING')
            assert 'NEXT_FIFO_INPUT' not in rendered
            self.implementation_seen.set()
            events = _text('redirect handled')
        elif len(self.requests) == 3:
            rendered = '\n'.join(part for message in request.compiled_input.messages for part in text_part_values(message.content))
            assert rendered.count('NEXT_FIFO_INPUT') == 1
            self.queued_seen.set()
            events = _text('queue handled')
        else:
            raise AssertionError('unexpected provider call')
        for event in events:
            yield event


async def _wait_for_card(session):
    async with asyncio.timeout(10):
        while session.live_control.current_snapshot().current_interaction is None:
            await asyncio.sleep(0.005)


@pytest.mark.parametrize(('capability', 'approved', 'detach', 'completed', 'redirect'),
    [(False, False, False, False, False), (True, False, False, False, False), (False, True, False, False, False),
     (False, False, True, False, False), (False, True, False, True, False), (False, False, True, False, True)],
    ids=['permission', 'capability-form', 'approved-before-dispatch', 'accepted-response-lost', 'physical-result-retained', 'queue-action-response-lost'])
def test_gateway_steer_closes_old_batch_then_delivers_fifo(tmp_path, monkeypatch, stage2_migrated_postgres_database, capability, approved, detach, completed, redirect):
    async def exercise():
        model = _BlockingModel(tmp_path, capability, completed)
        core, session = await _open_test_session(tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        running = None
        dispatch_entered, dispatch_release = asyncio.Event(), asyncio.Event()
        accepted, publish_release = asyncio.Event(), asyncio.Event()
        original_invoke = session._tools.invoke

        async def before_dispatch(**kwargs):
            result = await original_invoke(**kwargs) if completed else None
            dispatch_entered.set()
            await dispatch_release.wait()
            return result if completed else await original_invoke(**kwargs)

        if approved:
            monkeypatch.setattr(session._tools, 'invoke', before_dispatch)
        try:
            attachment = random_id('attachment')
            assert await session.attach_controller(attachment)
            running = asyncio.create_task(session.run_turn(PromptContent.text('prepare blocked work'),
                command_id=random_id('command'), requested_permission_mode=PermissionMode.ASK_PERMISSIONS))
            await _wait_for_card(session)
            original_card = session.live_control.current_snapshot().current_interaction
            queued = await session.submit_prompt(command_id=random_id('command'), content=PromptContent.text('NEXT_FIFO_INPUT'),
                requested_permission_mode=PermissionMode.READ_ONLY)
            assert queued.status == 'PENDING'
            assert session.live_control.current_snapshot().current_interaction == original_card
            if approved:
                snapshot = session.live_control.current_snapshot()
                await session.resolve_tool_interaction(
                    expected_writer_generation=session._lease.guard.writer_generation,
                    expected_owner_epoch=snapshot.owner_epoch, expected_live_revision=snapshot.revision,
                    interaction_id=original_card.interaction_id, command_id=random_id('command'),
                    decision='ALLOW', actor_id=attachment,
                )
                await asyncio.wait_for(dispatch_entered.wait(), 10)
            state = _Connection(attachment_id=attachment, attachment_generation=1,
                host_session=session, granted_role=wire.ATTACHMENT_ROLE_CONTROLLER, authenticated=True)
            gateway = TerminalKernelProtocolServer(socket_path=tmp_path/'protocol.sock', session_provider=lambda _: session)
            command_id = random_id('command')
            target = session._active_turn_id
            request = wire.CommandRequest(request_id=random_id('request'), command_id=command_id,
                command_kind=wire.STEER_ACTIVE_TURN, target_turn_id=target,
                prompt_content=wire.PromptContent(parts=[wire.PromptContentPart(text='STEER_KEEP_WORKING')]))
            if redirect:
                source = await session.submit_prompt(command_id=random_id('command'),
                    content=PromptContent.text('STEER_KEEP_WORKING'), requested_permission_mode=PermissionMode.READ_ONLY)
                request = wire.CommandRequest(request_id=random_id('request'), command_id=command_id,
                    command_kind=wire.STEER_QUEUED_PROMPT, target_turn_id=target,
                    target_queue_item_id=source.prompt_delivery.queue_item_id)
                original_io = session._io.run

                async def after_queue_commit(operation, *args, **kwargs):
                    outcome = await original_io(operation, *args, **kwargs)
                    if operation == session.repository.apply_queued_prompt_action:
                        accepted.set()
                        await publish_release.wait()
                    return outcome

                monkeypatch.setattr(session._io, 'run', after_queue_commit)
                caller = asyncio.create_task(gateway._command(state, request))
                await asyncio.wait_for(accepted.wait(), 10)
                caller.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await caller
                publish_release.set()
            elif detach:
                original_owner = session._submit_prompt_owner

                async def before_reply(**kwargs):
                    outcome = await original_owner(**kwargs)
                    accepted.set()
                    await publish_release.wait()
                    return outcome

                monkeypatch.setattr(session, '_submit_prompt_owner', before_reply)
                caller = asyncio.create_task(gateway._command(state, request))
                await asyncio.wait_for(accepted.wait(), 10)
                caller.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await caller
                publish_release.set()
            response = await gateway._command(state, request)
            assert response.command_outcome.status in (wire.PENDING, wire.SUCCEEDED)
            dispatch_release.set()
            await asyncio.wait_for(running, 10)
            await asyncio.wait_for(model.queued_seen.wait(), 10)
            assert model.implementation_seen.is_set()
            assert session.live_control.current_snapshot().current_interaction is None
            assert (tmp_path/'first.txt').exists() is completed
            assert not (tmp_path/'second.txt').exists()
            retry = await gateway._command(state, request)
            assert retry.command_outcome.prompt_delivery.queue_status == 'CONSUMED'
            with session.repository.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR,
                row_factory=dict_row, deadline_monotonic=monotonic()+10) as connection:
                rows = connection.execute('SELECT result_state FROM pulsara_v3.tool_results WHERE session_id=%s ORDER BY observed_at', (session.session_id,)).fetchall()
                assert [row['result_state'] for row in rows] == [
                    'SUCCESS' if completed else 'CANCELLED_BEFORE_DISPATCH', 'CANCELLED_BEFORE_DISPATCH']
                assert connection.execute('SELECT count(*) AS n FROM pulsara_v3.prompt_queue_items WHERE session_id=%s AND command_id=%s',
                    (session.session_id, command_id)).fetchone()['n'] == 1
        finally:
            publish_release.set()
            dispatch_release.set()
            await core.close_session(session.host_session_id)
            if running is not None:
                await asyncio.gather(running, return_exceptions=True)
            await core.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize('meta', [False, True], ids=['native-sdk', 'meta-sdk'])
@pytest.mark.parametrize('blocked_lane', [None, 'server', 'host'], ids=['permission-card', 'waiting-server-lane', 'waiting-host-lane'])
def test_real_stdio_mcp_steer_never_dispatches_effect(tmp_path, monkeypatch, stage2_migrated_postgres_database, meta, blocked_lane):
    import json
    import sys
    from tempfile import TemporaryDirectory
    from types import SimpleNamespace
    from pulsara_agent.desktop_app.renderer_bridge import RendererBridge
    from pulsara_agent.mcp_config import load_mcp_server_configs

    marker = tmp_path/'remote-effect.txt'
    server_file = tmp_path/'mcp_server.py'
    server_file.write_text('''from pathlib import Path
from mcp.server.mcpserver import MCPServer
import mcp_types as types
server = MCPServer("steer-regression")
@server.tool(annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
def write_mark(value: str) -> str:
    Path('''+repr(str(marker))+''').write_text(value)
    return value
if __name__ == "__main__":
    server.run()
''')
    config_path = tmp_path/'mcp.yaml'
    config_path.write_text('servers:\n  fixture:\n    required: true\n    enabled: true\n    catalog_refresh_interval_ms: DISABLED\n    transport: '+json.dumps({
        'type':'stdio', 'command':sys.executable, 'args':[str(server_file)],
    })+'\n')
    configs = load_mcp_server_configs(user_config_path=config_path)

    class Model(_DetachedDraftModel):
        def __init__(self):
            super().__init__()
            self.warmed = not meta
            self.inspected = False
            self.called = False
            self.finished = asyncio.Event()

        async def stream(self, request):
            self.requests.append(request)
            if not self.warmed:
                self.warmed = True
                events = _text('epoch installed before MCP discovery')
            elif meta and not self.inspected:
                self.inspected = True
                assert 'mcp__fixture__write_mark' not in {spec.name for spec in request.compiled_input.tools}
                events = _tool_call('inspect_capability', 'call:inspect-effect', {
                    'target':{'kind':'MCP_TOOL','server_id':'fixture','tool_name':'mcp__fixture__write_mark'},
                })
            elif not self.called:
                self.called = True
                name, arguments = 'mcp__fixture__write_mark', {'value':'must not reach MCP server'}
                if meta:
                    bodies = [text for message in request.compiled_input.messages for text in text_part_values(message.content)]
                    inspected = next(json.loads(json.loads(body)['pulsara_tool_result']['body'])
                        for body in reversed(bodies) if '"pulsara_tool_result"' in body)
                    assert inspected['invocation']['mode'] == 'META'
                    name, arguments = 'use_new_mcp_tool', {'tool_ref':inspected['tool_ref'], 'arguments':arguments}
                else:
                    assert name in {spec.name for spec in request.compiled_input.tools}
                events = _tool_call(name, 'call:external-effect', arguments)
            else:
                original = self.requests[0].compiled_input
                current = request.compiled_input
                assert original.system_prompt == current.system_prompt
                assert original.tools == current.tools
                rendered = '\n'.join(text for message in current.messages for text in text_part_values(message.content))
                assert rendered.index('被用户取消') < rendered.index('SDK_STEER_INPUT')
                self.finished.set()
                events = _text('external action cancelled before dispatch')
            for event in events:
                yield event

    async def exercise():
        model = Model()
        core, session = await _open_test_session(tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        running = None
        held_lane, held_count = None, 0
        socket_directory = TemporaryDirectory(prefix='steer-sdk-')
        gateway = TerminalKernelProtocolServer(socket_path=Path(socket_directory.name)/'socket', session_provider=lambda _: session)

        async def resume(_):
            return SimpleNamespace(session_id=session.session_id, host_session_id=session.host_session_id)

        bridge = RendererBridge(sessions=SimpleNamespace(resume_session=resume), protocol_server=gateway)
        try:
            await gateway.start()
            connection = await bridge.connect(session.session_id, browser_instance_id='00000000-0000-4000-8000-000000000001')
            if meta:
                await session.run_turn(PromptContent.text('warm'), command_id=random_id('command'),
                    requested_permission_mode=PermissionMode.ASK_PERMISSIONS)
            await session.reload_mcp_configs(configs, deadline_monotonic=monotonic()+20)
            await session._mcp_supervisor._wait_for_required_startup(('fixture',))
            session._tools.prepare_tool_surface_safe_point()
            if blocked_lane:
                slot = session._mcp_supervisor._slots['fixture']
                server_capacity, host_capacity = slot._lane._value, slot._host_lane._value
                held_lane = slot._lane if blocked_lane == 'server' else slot._host_lane
                held_count = held_lane._value
                for _ in range(held_count):
                    await held_lane.acquire()
            running = asyncio.create_task(session.run_turn(PromptContent.text('call effect'),
                command_id=random_id('command'), requested_permission_mode=PermissionMode.ASK_PERMISSIONS))
            await _wait_for_card(session)
            if blocked_lane:
                snapshot = session.live_control.current_snapshot()
                allowed = await bridge.resolve_interaction(connection['connection_id'], {
                    'command_id':random_id('command'), 'expected_writer_generation':session._lease.guard.writer_generation,
                    'expected_owner_epoch':snapshot.owner_epoch, 'expected_live_revision':snapshot.revision,
                    'interaction_id':snapshot.current_interaction.interaction_id, 'decision':'INTERACTION_ALLOW',
                })
                assert allowed['command_outcome']['status'] == 'SUCCEEDED'
                async with asyncio.timeout(10):
                    while not held_lane._waiters:
                        await asyncio.sleep(0.005)
            outcome = await bridge.command(connection['connection_id'], {
                'command_id': random_id('command'), 'command_kind': 'STEER_ACTIVE_TURN',
                'target_turn_id': session._active_turn_id,
                'prompt_content': {'parts': [{'type':'text', 'text':'SDK_STEER_INPUT'}]},
            })
            assert outcome['command_outcome']['status'] in ('PENDING','SUCCEEDED')
            await asyncio.wait_for(running,20)
            assert model.finished.is_set()
            assert not marker.exists()
            if held_lane is not None:
                assert held_lane._value == 0
                assert not held_lane._waiters
                assert slot._active_operation_count == 0
                assert not slot._permits
                assert slot._lane._value == (0 if blocked_lane == 'server' else server_capacity)
                assert slot._host_lane._value == (0 if blocked_lane == 'host' else host_capacity)
        finally:
            if held_lane is not None:
                for _ in range(held_count):
                    held_lane.release()
            await bridge.aclose()
            await gateway.close()
            socket_directory.cleanup()
            await core.close_session(session.host_session_id)
            if running is not None:
                await asyncio.gather(running, return_exceptions=True)
            await core.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize('approve', [False, True], ids=['cancel', 'approve'])
def test_pending_review_hook_observes_submission_but_permission_freezes_at_delivery(
    tmp_path, monkeypatch, stage2_migrated_postgres_database, approve,
):
    from tests.test_round9_2_hook_integration_postgres import (
        _HOOK_DRIVER, _command, _config, _install_trusted_sources, _log_values,
    )
    from pulsara_agent.primitives.plan_workflow import PlanDraftDecision

    home = tmp_path/'home'
    driver, log = tmp_path/'hook.py', tmp_path/'hook.jsonl'
    driver.write_text(_HOOK_DRIVER)
    _install_trusted_sources(home=home, workspace_root=tmp_path,
        user_config=_config(_command(driver, log, 'USER'), ('UserPromptSubmit',)), workspace_config=None)
    monkeypatch.setenv('PULSARA_HOME', str(home))

    class Model(_DetachedDraftModel):
        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                events = _tool_call('exit_plan', 'call:hook-draft', {'plan':'1. review', 'summary':'review'})
            else:
                rendered = '\n'.join(text for message in request.compiled_input.messages for text in text_part_values(message.content))
                if 'AFTER_REVIEW_QUEUE' in rendered:
                    self.implementation_seen.set()
                events = _text('complete')
            for event in events:
                yield event

    async def exercise():
        model = Model()
        core, session = await _open_test_session(tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        try:
            entered = await session.enter_plan(command_id=random_id('command'), entry_reason='review',
                resume_permission_mode=PermissionMode.ACCEPT_EDITS)
            await session.run_turn(PromptContent.text('prepare'), command_id=random_id('command'),
                requested_permission_mode=PermissionMode.ACCEPT_EDITS)
            command_id = random_id('command')
            accepted = await session.submit_prompt(command_id=command_id, content=PromptContent.text('AFTER_REVIEW_QUEUE'),
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            assert accepted.prompt_delivery.queue_status == 'PENDING'
            submissions = [row['stdin'] for row in _log_values(log) if row['stdin'].get('prompt') == 'AFTER_REVIEW_QUEUE']
            assert len(submissions) == 1
            assert submissions[0]['permission_mode'] == 'plan'
            with session.repository.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR,
                row_factory=dict_row, deadline_monotonic=monotonic()+10) as connection:
                review = connection.execute("SELECT i.id, w.workflow_revision FROM pulsara_v3.plan_interactions i JOIN pulsara_v3.plan_workflows w ON w.id=i.plan_workflow_id WHERE i.session_id=%s AND i.kind='DRAFT_REVIEW' AND i.status='OPEN'", (session.session_id,)).fetchone()
            await session.resolve_plan_draft_review(command_id=random_id('command'), workflow_id=entered.target_id,
                expected_workflow_revision=review['workflow_revision'], interaction_id=review['id'],
                decision=PlanDraftDecision.APPROVE if approve else PlanDraftDecision.CANCEL)
            await asyncio.wait_for(model.implementation_seen.wait(), 10)
            retry = await session.submit_prompt(command_id=command_id, content=PromptContent.text('AFTER_REVIEW_QUEUE'),
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            assert retry.prompt_delivery.queue_status == 'CONSUMED'
            submissions = [row['stdin'] for row in _log_values(log) if row['stdin'].get('prompt') == 'AFTER_REVIEW_QUEUE']
            assert len(submissions) == 1
            with session.repository.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR,
                row_factory=dict_row, deadline_monotonic=monotonic()+10) as connection:
                turn = connection.execute('SELECT t.requested_permission_mode, t.effective_permission_mode, t.permission_overlay FROM pulsara_v3.prompt_queue_items q JOIN pulsara_v3.turns t ON t.initial_entry_id=q.consumed_entry_id AND t.session_id=q.session_id WHERE q.session_id=%s AND q.command_id=%s',
                    (session.session_id, command_id)).fetchone()
                assert turn == {'requested_permission_mode':'bypass-permissions', 'effective_permission_mode':'bypass-permissions', 'permission_overlay':'NONE'}
        finally:
            await core.close_session(session.host_session_id)
            await core.shutdown()

    asyncio.run(exercise())


def test_queue_to_plan_question_steer_survives_lost_commit_response(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    from tests.test_round4_plan_host import _QuestionSteerModel

    async def exercise():
        model = _QuestionSteerModel()
        core, session = await _open_test_session(tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        committed, release = asyncio.Event(), asyncio.Event()
        running = caller = None
        try:
            await session.enter_plan(command_id=random_id('command'), entry_reason='question',
                resume_permission_mode=PermissionMode.ACCEPT_EDITS)
            running = asyncio.create_task(session.run_turn(PromptContent.text('ask'),
                command_id=random_id('command'), requested_permission_mode=PermissionMode.ACCEPT_EDITS))
            async with asyncio.timeout(10):
                while await session._plan_interactions.current_open() is None:
                    await asyncio.sleep(0.005)
            queued = await session.submit_prompt(command_id=random_id('command'), content=PromptContent.text('QUEUE_QUESTION_REDIRECT'),
                requested_permission_mode=PermissionMode.READ_ONLY)
            command_id = random_id('command')
            target = session._active_turn_id
            original_io = session._io.run

            async def after_commit(operation, *args, **kwargs):
                result = await original_io(operation, *args, **kwargs)
                if operation == session.repository.apply_queued_prompt_action:
                    committed.set()
                    await release.wait()
                return result

            monkeypatch.setattr(session._io, 'run', after_commit)
            caller = asyncio.create_task(session.steer_queued_prompt(command_id=command_id,
                source_queue_item_id=queued.prompt_delivery.queue_item_id, target_turn_id=target))
            await asyncio.wait_for(committed.wait(), 10)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            assert await session._plan_interactions.current_open() is not None
            release.set()
            await asyncio.wait_for(running, 10)
            assert model.implementation_seen.is_set()
            assert await session._plan_interactions.current_open() is None
            retry = await session.steer_queued_prompt(command_id=command_id,
                source_queue_item_id=queued.prompt_delivery.queue_item_id, target_turn_id=target)
            assert retry.prompt_delivery.queue_status == 'CONSUMED'
            rendered = '\n'.join(text for message in model.requests[-1].compiled_input.messages for text in text_part_values(message.content))
            assert rendered.count('QUEUE_QUESTION_REDIRECT') == 1
            assert rendered.index('被用户取消') < rendered.index('QUEUE_QUESTION_REDIRECT')
        finally:
            release.set()
            await core.close_session(session.host_session_id)
            await asyncio.gather(*(task for task in (running, caller) if task is not None), return_exceptions=True)
            await core.shutdown()

    asyncio.run(exercise())
