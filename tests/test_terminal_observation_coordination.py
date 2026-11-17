"""Concurrent model observations consume only accepted process output."""
import asyncio
from time import monotonic
from types import SimpleNamespace
import pytest

from pulsara_agent.conversation_kernel.tool_contracts import KernelToolResult
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort, _DirectTerminalProcessTool
from pulsara_agent.identity import random_id
from pulsara_agent.ports.tool_execution import ToolCall
from pulsara_agent.terminal_process import TerminalManager, TerminalRequest
from pulsara_agent.terminal_process.models import TerminalProcessOrigin
from pulsara_agent.terminal_process.output import TerminalOutputOwner


def test_concurrent_observations_wait_for_acceptance_and_failed_save_releases(tmp_path):
    async def scenario():
        manager = TerminalManager(tmp_path)
        owner, session = random_id('host'), random_id('session')
        manager.activate_owner(owner)
        initial = asyncio.Event()
        try:
            first = await asyncio.to_thread(manager.execute, TerminalRequest('printf pending'),
                owner_host_session_id=owner, origin=TerminalProcessOrigin(random_id('turn'), 'ROOT', session),
                decision_deadline_monotonic=monotonic()+30, initial_observation=initial)
            target = {'kind':'PROCESS', 'process_id':first.process_id}
            tool = _DirectTerminalProcessTool(manager, owner, session_id=session)
            args = {'target':target, 'action':'wait', 'timeout_seconds':0}
            calls = []

            async def physical(**kwargs):
                calls.append(1)
                value = await asyncio.to_thread(tool.execute, ToolCall(random_id('call'), 'manage_process', args))
                return KernelToolResult('SUCCESS', value.business_value, acceptance_succeeded=value.acceptance_succeeded)

            port = SimpleNamespace(_invoke_physical_tool_uncoordinated=physical)
            async def invoke():
                return await DirectKernelToolPort._invoke_physical_tool(port, tool=tool,
                    call=ToolCall(random_id('call'), 'manage_process', args), tool_name='manage_process',
                    arguments=args, turn_id='turn', attempt_id='attempt', invocation_context=None,
                    live_sink=None, observation_origin=None, cancel_requested=None)

            # query_processes can expose the process before the initial result is accepted.
            a = asyncio.create_task(invoke())
            await asyncio.sleep(.02)
            assert not calls
            initial.set()  # initial save failed; its output remains pending
            first_wait = await a
            assert first_wait.content.content[0].value == 'pending'
            b = asyncio.create_task(invoke())
            await asyncio.sleep(.02)
            assert len(calls) == 1
            b.cancel()
            with pytest.raises(asyncio.CancelledError):
                await b
            b = asyncio.create_task(invoke())
            first_wait.acceptance_finished()  # save failed: release without advancing
            second_wait = await b
            assert second_wait.content.content[0].value == 'pending'
            c = asyncio.create_task(invoke())
            await asyncio.sleep(.02)
            assert len(calls) == 2
            await asyncio.to_thread(second_wait.acceptance_succeeded)
            second_wait.acceptance_finished()
            third_wait = await c
            assert not third_wait.content.content
            third_wait.acceptance_finished()

            initial2 = asyncio.Event()
            another = await asyncio.to_thread(manager.execute, TerminalRequest('printf initial'),
                owner_host_session_id=owner, origin=TerminalProcessOrigin(random_id('turn'), 'ROOT', session),
                decision_deadline_monotonic=monotonic()+30, initial_observation=initial2)
            args = {'target':{'kind':'PROCESS', 'process_id':another.process_id}, 'action':'wait', 'timeout_seconds':0}
            waiter = asyncio.create_task(invoke())
            await asyncio.sleep(.02)
            assert not waiter.done()
            await asyncio.to_thread(manager.accept_model_observation, another.process_id, another.output_cursor,
                owner_host_session_id=owner)
            initial2.set()
            after_initial = await waiter
            assert not after_initial.content.content
            after_initial.acceptance_finished()
        finally:
            manager.release_owner_and_join(owner)
    asyncio.run(scenario())


def test_reclaim_failure_does_not_reobserve_accepted_output(monkeypatch):
    import pulsara_agent.terminal_process.output as output_module
    owner = TerminalOutputOwner(owner_epoch=random_id('host'), process_id=random_id('process'))
    owner.append_raw(b'accepted')
    cursor = owner.capture_cursor()
    def fail(**_kwargs):
        raise OSError('temporary storage full')
    monkeypatch.setattr(output_module, 'SpooledTemporaryFile', fail)
    owner.accept_observation(cursor)
    assert owner.observation_cursor() == cursor


def test_reclaim_close_failure_keeps_new_spool_usable():
    owner = TerminalOutputOwner(owner_epoch=random_id('host'), process_id=random_id('process'))
    owner.append_raw(b'first\n')
    cursor = owner.capture_cursor()
    old = owner._retained
    class FailingClose:
        def __getattr__(self, name):
            return getattr(old, name)
        def close(self):
            old.close()
            raise OSError('close failed')
    owner._retained = FailingClose()
    owner.accept_observation(cursor)
    owner.append_raw(b'second\n')
    assert owner.snapshot(maximum_chars=None, since_cursor=owner.observation_cursor()).text == 'second\n'


@pytest.mark.postgres
@pytest.mark.parametrize('fail_save', [False, True])
def test_code_mode_parallel_waits_consume_output_once(tmp_path, monkeypatch, stage2_migrated_postgres_database, fail_save):
    import json
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    import pulsara_agent.conversation_kernel.host as kernel_host
    from pulsara_agent.llm.input import PromptContent
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.model_config import test_model_runtime, test_model_binding
    from tests.support.round3 import CallbackScriptedKernelModel
    from tests.support.model_stream import _tool_call, _text
    from tests.test_agent_code_mode_execution import _settled_formal_records

    if fail_save:
        from pulsara_agent.conversation_kernel.tool_outputs import ToolOutputProcessor
        original_prepare = ToolOutputProcessor.prepare
        failed = []
        def prepare(*, business_value, result_facts=None):
            if not failed and any(part.type == 'text' and 'COORDINATED' in part.value
                                  for part in business_value.content):
                failed.append(True)
                raise OSError('injected result save failure')
            return original_prepare(business_value=business_value, result_facts=result_facts)
        monkeypatch.setattr(ToolOutputProcessor, 'prepare', staticmethod(prepare))

    code = '''const p = await tools.exec_process({command:"read x; printf COORDINATED",yield_time_ms:0});
const calls = [{target:p.target,action:"write",data:"go\\n",yield_time_ms:1000},
               {target:p.target,action:"wait",timeout_seconds:1}];
const observe = args => tools.manage_process(args).then(
  r => r.content.map(x => x.value).join(""), _error => "SAVE_FAILED");
const results = await Promise.all(calls.map(observe));
results.push(await observe({target:p.target,action:"wait",timeout_seconds:1}));
console.log(JSON.stringify(results));'''
    async def stream(_request):
        payloads = (_tool_call('exec_cell', random_id('call'), {'code':code})
                    if len(model.requests) == 1 else _text(random_id('block'), 'DONE'))
        for payload in payloads:
            yield payload
    model = CallbackScriptedKernelModel(stream)
    monkeypatch.setattr(kernel_host, 'DirectKernelModelPort', lambda **_: model)
    monkeypatch.setattr(kernel_host.LocalMcpManagementService, 'load_configs', lambda *_a, **_k: ())
    runtime = test_model_runtime(model_id='test-pro', wire_api='openai_chat_completions',
        postgres_dsn=stage2_migrated_postgres_database.runtime_dsn)
    async def scenario():
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(HostWorkspaceInput(workspace_kind='project', workspace_root=tmp_path))
        try:
            await session.update_model_call_binding(test_model_binding(runtime))
            await session.run_turn(PromptContent.text('execute'), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            records = await _settled_formal_records(session, expected=1)
            expected = ['COORDINATED', 'SAVE_FAILED'] if fail_save else ['COORDINATED']
            assert sorted(value for value in json.loads(records[0]['text']) if value) == expected
            if fail_save:
                assert failed == [True]
        finally:
            await core.shutdown()
    asyncio.run(scenario())
