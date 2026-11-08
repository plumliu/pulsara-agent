"""Session-local user actions are lossy facts, independent of physical control."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace

from tests.test_pr03_user_controls import _Clock, _command, _host


def _process(process_id, *, origin="ROOT", agent=None):
    return SimpleNamespace(
        process_id=process_id,
        origin=SimpleNamespace(conversation_scope_kind=origin, session_id=agent),
    )


def test_user_action_batch_merges_and_take_leaves_later_results():
    async def run():
        host = _host(_Clock())
        host.workspace_gate = SimpleNamespace(wake=lambda: None)
        host._queue_wake = asyncio.Event()
        first = 'process:34npgaljkboupr4n456vrmou6a'
        second = 'process:qmi3ybigp4h4iv3tmaqmlm64am'
        agent = 'session:5dyf7lfzp7z22pphjtostkgsjm'
        results = {
            first: SimpleNamespace(disposition=SimpleNamespace(value='TERMINATION_COMPLETED'), result=SimpleNamespace(status=SimpleNamespace(value='killed'), exit_code=-15), physical_state='TERMINAL', group_alive=False),
            second: SimpleNamespace(disposition=SimpleNamespace(value='PHYSICAL_SETTLEMENT_INCOMPLETE'), result=SimpleNamespace(status=SimpleNamespace(value='unknown'), exit_code=None), physical_state='unknown', group_alive=None),
        }
        class Tools:
            def list_background_terminal_processes(self):
                return [_process(first), _process(second, origin='SUBAGENT', agent=agent)]
            def terminate_background_terminal_process(self, process_id):
                return results[process_id]
        host._tools = Tools()
        await host.record_workspace_recreated(datetime(2026, 1, 2, tzinfo=timezone.utc))
        for idx, process_id in enumerate((second, first)):
            outcome = await host.request_terminate_background_process(
                command_id=_command(host, str(idx)), process_id=process_id,
                expected_session_id=host.session_id,
                expected_host_session_id=host.host_session_id,
                admission_deadline_ms=host.control_admission_deadline_ms(),
            )
            await host._user_control_attempts[outcome.command_id].task
        results[first] = SimpleNamespace(
            disposition=SimpleNamespace(value='PHYSICAL_SETTLEMENT_INCOMPLETE'),
            result=SimpleNamespace(status=SimpleNamespace(value='unknown'), exit_code=None),
            physical_state='unknown', group_alive=None,
        )
        repeated = await host.request_terminate_background_process(
            command_id=_command(host, 'later'), process_id=first,
            expected_session_id=host.session_id, expected_host_session_id=host.host_session_id,
            admission_deadline_ms=host.control_admission_deadline_ms(),
        )
        await host._user_control_attempts[repeated.command_id].task
        batch = json.loads(await host.take_user_actions())['actions']
        assert [x['kind'] for x in batch] == ['workspace_recreated', 'process_control', 'process_control']
        assert batch[0]['original_files_restored'] is False
        assert [x['target']['process_id'] for x in batch[1:]] == sorted((first, second))
        assert next(x for x in batch if x.get('target', {}).get('process_id') == first)['status'] == 'unknown'
        agent_item = next(x for x in batch if x.get('target', {}).get('process_id') == second)
        assert agent_item['origin'] == {'kind': 'AGENT', 'agent_id': agent}
        assert agent_item['disposition'] == 'PHYSICAL_SETTLEMENT_INCOMPLETE'
        assert 'public_detail' in agent_item
        assert await host.take_user_actions() is None
        await host.record_workspace_recreated(datetime(2026, 1, 3, tzinfo=timezone.utc))
        assert json.loads(await host.take_user_actions())['actions'][0]['created_at'].startswith('2026-01-03')
    asyncio.run(run())


def test_termination_failure_reports_unknown_and_closed_host_drops_notice():
    async def run():
        host = _host(_Clock())
        process_id = 'process:34npgaljkboupr4n456vrmou6a'
        class Tools:
            def list_background_terminal_processes(self): return [_process(process_id)]
            def terminate_background_terminal_process(self, _): raise OSError('operation unavailable')
        host._tools = Tools()
        result = await host.request_terminate_background_process(
            command_id=_command(host, 'failed'), process_id=process_id,
            expected_session_id=host.session_id, expected_host_session_id=host.host_session_id,
            admission_deadline_ms=host.control_admission_deadline_ms(),
        )
        attempt = host._user_control_attempts[result.command_id]
        await attempt.task
        assert attempt.outcome.status == 'FAILED'
        action = json.loads(await host.take_user_actions())['actions'][0]
        assert action['disposition'] == 'OWNER_UNAVAILABLE'
        assert action['status'] == 'unknown'
        assert action['public_detail'] == 'operation unavailable'
        await host._execute_terminate_process_attempt(attempt)
        assert host._pending_process_controls
        host._closing = True
        assert await host.take_user_actions() is None
        assert not host._pending_process_controls
        await host._execute_terminate_process_attempt(attempt)
        assert await host.take_user_actions() is None
    asyncio.run(run())


def test_action_dynamic_text_uses_saved_and_process_credential_boundaries():
    from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
    from tests.support.model_config import test_model_runtime

    async def run():
        saved_secret = 'fixture-saved-api-secret'
        process_secret = 'fixture-runtime-secret'
        host = _host(_Clock())
        host._model_runtime = test_model_runtime(api_key=saved_secret)
        host._credential_boundary = ProcessCredentialBoundary(process_secret)
        host.workspace = SimpleNamespace(workspace_root=f'/tmp/keep-{saved_secret}-and-{process_secret}/workspace')
        host.workspace_gate = SimpleNamespace(wake=lambda: None)
        host._queue_wake = asyncio.Event()
        process_id = 'process:34npgaljkboupr4n456vrmou6a'

        class Tools:
            def list_background_terminal_processes(self): return [_process(process_id)]
            def terminate_background_terminal_process(self, _):
                raise OSError(f'failure before {saved_secret} and {process_secret}; retry manually')

        host._tools = Tools()
        await host.record_workspace_recreated(datetime(2026, 1, 2, tzinfo=timezone.utc))
        pending = await host.request_terminate_background_process(
            command_id=_command(host, 'credentials'), process_id=process_id,
            expected_session_id=host.session_id, expected_host_session_id=host.host_session_id,
            admission_deadline_ms=host.control_admission_deadline_ms(),
        )
        attempt = host._user_control_attempts[pending.command_id]
        await attempt.task
        assert attempt.outcome.status == 'FAILED'
        assert saved_secret in attempt.outcome.public_message
        body = await host.take_user_actions()
        assert body is not None
        assert saved_secret not in body and process_secret not in body
        workspace, process = json.loads(body)['actions']
        assert workspace['workspace_root'].startswith('/tmp/keep-')
        assert workspace['workspace_root'].endswith('/workspace')
        assert process['public_detail'].startswith('failure before ')
        assert process['public_detail'].endswith('; retry manually')
        assert process['target']['process_id'] == process_id
        assert process['disposition'] == 'OWNER_UNAVAILABLE'
    asyncio.run(run())


def test_projection_unavailable_drops_batch_without_changing_control_result():
    async def run():
        host = _host(_Clock())
        host._model_runtime = SimpleNamespace(settings=SimpleNamespace(
            read=lambda: (_ for _ in ()).throw(OSError('fixture unavailable'))
        ))
        process_id = 'process:34npgaljkboupr4n456vrmou6a'
        class Tools:
            def list_background_terminal_processes(self): return [_process(process_id)]
            def terminate_background_terminal_process(self, _): raise OSError('actual failure')
        host._tools = Tools()
        pending = await host.request_terminate_background_process(
            command_id=_command(host, 'unavailable'), process_id=process_id,
            expected_session_id=host.session_id, expected_host_session_id=host.host_session_id,
            admission_deadline_ms=host.control_admission_deadline_ms(),
        )
        attempt = host._user_control_attempts[pending.command_id]
        await attempt.task
        assert await host.take_user_actions() is None
        assert await host.take_user_actions() is None
        assert attempt.outcome.status == 'FAILED'
        assert attempt.outcome.public_message == 'actual failure'
    asyncio.run(run())


def test_cancelled_projection_leaves_newer_actions_untouched():
    import threading
    import pytest

    async def run():
        host = _host(_Clock())
        host.workspace_gate = SimpleNamespace(wake=lambda: None)
        host._queue_wake = asyncio.Event()
        entered, release, exited = (threading.Event() for _ in range(3))
        settings = host._model_runtime.settings

        def blocked_read():
            entered.set()
            try:
                assert release.wait(5)
                return settings.read()
            finally:
                exited.set()

        host._model_runtime = SimpleNamespace(settings=SimpleNamespace(read=blocked_read))
        await host.record_workspace_recreated(datetime(2026, 1, 2, tzinfo=timezone.utc))
        first = asyncio.create_task(host.take_user_actions())
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await host.record_workspace_recreated(datetime(2026, 1, 3, tzinfo=timezone.utc))
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        finally:
            release.set()
            assert await asyncio.to_thread(exited.wait, 2)
        second = json.loads(await host.take_user_actions())['actions']
        assert second[0]['created_at'].startswith('2026-01-03')
        assert await host.take_user_actions() is None
    asyncio.run(run())
