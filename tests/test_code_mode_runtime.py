"""Real fixed QuickJS SDK integration: cells never use Python/Node eval fallbacks."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from time import monotonic
from types import SimpleNamespace

import pytest

from pulsara_agent.code_mode import (
    CodeModeError, CodeModeHost, CodeModeLimits, CodeModeToolResult,
)


async def _start(host, source, invoke=None, catalog=None, **kwargs):
    async def empty(_):
        return CodeModeToolResult(value={})
    captured = getattr(host, "_test_cells", {})
    host._test_cells = captured
    closed = kwargs.pop("on_cell_closed", None)
    def on_closed(identity):
        captured[identity] = host._cell(identity)
        if closed is not None:
            closed(identity)
    identity = await host.start(source=source, source_exec_attempt_id="test-source-attempt",
                            turn_id=kwargs.pop("turn_id", "test-turn"), snapshot=object(),
                            tool_catalog=catalog or {"read_file": {}}, invoke=invoke or empty,
                            on_cell_closed=on_closed, **kwargs)
    if identity in host._cells:
        captured[identity] = host._cell(identity)
    return identity


async def _finish(host, cell):
    records = []
    observed_errors = []
    owned = host._test_cells[cell]
    async with asyncio.timeout(15):
        while True:
            if cell in host._cells:
                lease = await host.lease_output(cell)
                if lease is not None:
                    if lease.record["kind"] == "error":
                        observed_errors.append(lease.record["error"])
                    else:
                        records.append(dict(lease.record))
                    await host.acknowledge_output(lease)
                    continue
            if owned.state in {"completed", "failed", "terminated"} and not owned.pending and not owned.output:
                if owned.error is not None and not owned.explicitly_stopped:
                    assert observed_errors, "An unexpected failure must have delivered its real diagnostic"
                return SimpleNamespace(state=owned.state, error=owned.error, pending_calls=owned.pending_calls), records
            await asyncio.sleep(.001)


async def _vm_terminal(host, cell):
    owned = host._test_cells[cell]
    async with asyncio.timeout(15):
        while owned.state not in {"completed", "failed", "terminated"}:
            await asyncio.sleep(.001)


def test_real_quickjs_isolation_and_detached_tool_microtasks():
    async def run():
        host = CodeModeHost()
        calls = []
        async def invoke(call):
            calls.append(call)
            await asyncio.sleep(.01)
            return CodeModeToolResult(value=call.arguments["value"])
        try:
            cell = await _start(host, '''
              globalThis.privateCellValue = 17;
              if ([typeof process, typeof require, typeof fetch, typeof setTimeout].some(x => x !== 'undefined'))
                throw new Error('Node API leaked');
              tools.read_file({value:1}).then(v => {
                console.log('first',v); return tools.read_file({value:2});
              }).then(v => console.log('second',v));
              return {answer:42};
            ''', invoke)
            page, output = await _finish(host, cell)
            assert page.state == "completed"
            assert [(r["kind"], r["text"]) for r in output] == [
                ("log", "first 1"), ("log", "second 2"), ("return", '{"answer":42}')]
            assert [call.sequence for call in calls] == [1, 2]
            other = await _start(host, 'return typeof privateCellValue;')
            _, output = await _finish(host, other)
            assert output == [{"kind": "return", "text": "undefined"}]
        finally:
            await host.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("source, failed", [
    ('const p=Promise.reject(new Error("temporary")); p.catch(()=>{}); return "ok";', False),
    ('const p=Promise.reject(new Error("temporary")); Promise.resolve().then(()=>p.catch(()=>{})); return "ok";', False),
    ('Promise.reject(new Error("unhandled")); return "ok";', True),
    ('await new Promise(()=>{});', True),
])
def test_unhandled_rejections_after_full_job_drain_and_stalled(source, failed):
    async def run():
        host = CodeModeHost()
        try:
            page, output = await _finish(host, await _start(host, source))
            assert (page.state == "failed") is failed
            if failed:
                assert page.error
                assert not any(record["kind"] == "return" for record in output)
        finally:
            await host.aclose()
    asyncio.run(run())


def test_unawaited_tool_rejection_is_not_hidden_by_fulfilled_root():
    async def run():
        host = CodeModeHost()
        async def invoke(_):
            await asyncio.sleep(.01)
            return CodeModeToolResult(value={"detail": "actual failure"}, state="FAILED",
                                      error_code="tool_failed", error_message="Actual failure",
                                      result_id="actual-result")
        try:
            page, output = await _finish(host, await _start(host, 'tools.read_file({}); return "success";', invoke))
            assert page.state == "failed"
            assert page.error["name"] == "ToolError"
            assert page.error["code"] == "tool_failed"
            assert page.error["result"] == {"detail": "actual failure"}
            assert page.error["result_id"] == "actual-result"
            assert not output
        finally:
            await host.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("expression", [
    '{a:undefined}', '{a:NaN}', '{a:Infinity}', '{a:9007199254740992}',
    '{a:1n}', '{a:()=>{}}', '{a:Symbol()}', '{a:new Date()}', '[,1]',
    '(()=>{let x={};x.x=x;return x})()', '{toJSON(){return {silently:"changed"}}}',
])
def test_strict_json_rejects_before_business_execution(expression):
    async def run():
        host = CodeModeHost()
        calls = []
        async def invoke(call):
            calls.append(call)
            return CodeModeToolResult(value={})
        try:
            page, _ = await _finish(host, await _start(host, f'await tools.read_file({expression});', invoke))
            assert page.state == "failed"
            assert not calls
        finally:
            await host.aclose()
    asyncio.run(run())


def test_safe_numbers_and_text_have_distinct_bridge_semantics():
    async def run():
        host = CodeModeHost()
        async def invoke(call):
            if call.arguments["case"] == "text":
                return CodeModeToolResult(value='{"unsafe":9007199254740993}')
            return CodeModeToolResult(value={"unsafe": 9_007_199_254_740_993}, result_id="retained-result")
        try:
            page, output = await _finish(host, await _start(host, '''
                console.log(await tools.read_file({case:'text'}));
                try { await tools.read_file({case:'number'}); }
                catch(e) { console.log(e.name,e.code,e.result.result_id); }
            ''', invoke))
            assert page.state == "completed"
            assert [record["text"] for record in output] == [
                '{"unsafe":9007199254740993}', 'ToolError unsafe_integer retained-result']
        finally:
            await host.aclose()
    asyncio.run(run())


def test_external_wait_is_not_vm_budget_and_short_segments_are_not_accumulated():
    async def run():
        limits = replace(CodeModeLimits(), segment_ms=80, watchdog_ms=300)
        host = CodeModeHost(limits=limits)
        async def invoke(_):
            await asyncio.sleep(.12)
            return CodeModeToolResult(value=1)
        try:
            page, output = await _finish(host, await _start(host, '''
              for(let i=0;i<4;i++) {
                const until=Date.now()+12;while(Date.now()<until){}
                await tools.read_file({});
              }
              console.log('done');
            ''', invoke))
            assert page.state == "completed", page.error
            assert output == [{"kind": "log", "text": "done"}]
        finally:
            await host.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("source", ['while(true){}', 'while(true){await Promise.resolve();}'])
def test_loops_and_infinite_microtasks_share_continuous_segment(source):
    async def run():
        host = CodeModeHost(limits=replace(CodeModeLimits(), segment_ms=50, watchdog_ms=250))
        try:
            started = monotonic()
            page, _ = await _finish(host, await _start(host, source))
            assert page.state == "failed"
            assert monotonic() - started < 2
            assert "bound" in page.error["message"] or "interrupt" in page.error["message"]
        finally:
            await host.aclose()
    asyncio.run(run())


def test_stop_is_external_and_preserves_actual_late_settlement():
    async def run():
        host = CodeModeHost()
        dispatched, settle = asyncio.Event(), asyncio.Event()
        calls = []
        async def invoke(call):
            calls.append(call)
            dispatched.set()
            await settle.wait()
            return CodeModeToolResult(value={"actual": "settled"}, result_id="accepted-result")
        try:
            cell = await _start(host, 'await tools.read_file({});console.log("too late");await tools.read_file({});', invoke)
            await dispatched.wait()
            stopped = await host.stop(cell)
            assert stopped["state"] == "terminated"
            assert calls[0].cancelled.is_set()
            settle.set()
            page, output = await _finish(host, cell)
            assert page.state == "terminated"
            assert len(calls) == 1
            assert not output  # Explicit Stop settles effects without another model input.
        finally:
            settle.set()
            await host.aclose()
    asyncio.run(run())


def test_vm_slots_queue_sources_and_stop_queued_cell_without_evaluation():
    async def run():
        host = CodeModeHost(limits=replace(CodeModeLimits(), vm_slots=1))
        dispatched, settle = asyncio.Event(), asyncio.Event()
        async def invoke(_):
            dispatched.set()
            await settle.wait()
            return CodeModeToolResult(value={})
        try:
            first = await _start(host, 'await tools.read_file({});', invoke)
            await dispatched.wait()
            second = await _start(host, 'throw new Error("must not evaluate");')
            page = await host.wait_state(second, 0)
            assert page["state"] == "queued"
            assert "output" not in page
            assert (await host.stop(second))["state"] == "terminated"
            page, output = await _finish(host, second)
            assert not output and not page.error
            settle.set()
            assert (await _finish(host, first))[0].state == "completed"
        finally:
            settle.set()
            await host.aclose()
    asyncio.run(run())


def test_each_output_has_immutable_independent_admission_identity():
    async def run():
        host = CodeModeHost()
        wakes = []
        host.set_output_ready_callback(lambda: wakes.append(True))
        try:
            cell = await _start(host, 'for(let i=0;i<5;i++)console.log(String(i)+"x".repeat(18000));')
            await _vm_terminal(host, cell)
            identities, records = [], []
            while cell in host.pending_output_cells():
                lease = await host.lease_output(cell)
                assert await host.lease_output(cell) is lease
                identities.append(lease.record_sequence)
                records.append(lease.record)
                with pytest.raises(CodeModeError, match="live output"):
                    await host.acknowledge_output(lease, accepted=False)
                await host.acknowledge_output(lease)
            assert identities == list(range(1, 6))
            assert [record["text"][0] for record in records] == list("01234")
            assert wakes
        finally:
            await host.aclose()
    asyncio.run(run())


def test_large_output_remains_complete_for_canonical_queue_content_owner():
    async def run():
        host = CodeModeHost()
        try:
            cell = await _start(host, 'console.log("x".repeat(60000));')
            await _vm_terminal(host, cell)
            lease = await host.lease_output(cell)
            assert lease.record == {"kind": "log", "text": "x" * 60000}
            assert lease.source_exec_attempt_id == "test-source-attempt"
            assert lease.source_turn_id == "test-turn"
            await host.acknowledge_output(lease)
        finally:
            await host.aclose()
    asyncio.run(run())


def test_output_full_fails_explicitly_and_keeps_every_accepted_record():
    async def run():
        limits = replace(CodeModeLimits(), value_bytes=8192, cell_bytes=32768, host_bytes=65536, control_bytes=12288, page_bytes=8192)
        host = CodeModeHost(limits=limits)
        try:
            cell = await _start(host, 'for(let i=0;i<100;i++)console.log(String(i)+":"+"x".repeat(1000));')
            await _vm_terminal(host, cell)
            page, output = await _finish(host, cell)
            assert page.state == "failed" and page.error
            assert "bound" in page.error["message"]
            assert 0 < len(output) < 100
            assert [int(item["text"].split(':')[0]) for item in output] == list(range(len(output)))
        finally:
            await host.aclose()
    asyncio.run(run())


def test_batch_preallocates_frozen_refs_then_one_real_descriptor():
    async def run():
        host = CodeModeHost()
        calls = []
        async def invoke(call):
            calls.append(call)
            if call.tool_name == 'run_agent.batch':
                return CodeModeToolResult(value={m['key']:{'agent':{'kind':'AGENT','agent_id':m['agent_id']},
                    'work':{'kind':'WORK','work_id':m['work_id']}} for m in call.arguments['members']})
            return CodeModeToolResult(value={'state':'settled'})
        try:
            page, output = await _finish(host, await _start(host, '''
                const result=await tools.run_agent.batch({
                  first:{instruction:({second})=>JSON.stringify(second.agent)},
                  second:{instruction:'Review',depends_on:({first})=>[first.work]}
                });
                console.log(await tools.wait_agent.works({targets:[result.second.work]}));
            ''', invoke, {'run_agent':{}, 'wait_agent':{}}))
            assert page.state == 'completed', page.error
            assert [call.tool_name for call in calls] == ['run_agent.batch', 'wait_agent.works']
            assert [call.sequence for call in calls] == [1, 2]
            a, b = calls[0].arguments['members']
            assert json.loads(a['parameters']['instruction'])['agent_id'] == b['agent_id']
            assert b['parameters']['depends_on'] == [{'kind':'WORK','work_id':a['work_id']}]
            assert output == [{'kind':'log','text':'{"state":"settled"}'}]
        finally:
            await host.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('instruction', [
    'async peers=>"async is illegal"',
    'peers=>{tools.read_file({});return "side effect"}',
])
def test_batch_functions_are_synchronous_and_cannot_submit_tools(instruction):
    async def run():
        host = CodeModeHost()
        calls = []
        async def invoke(call):
            calls.append(call)
            return CodeModeToolResult(value={})
        try:
            page, _ = await _finish(host, await _start(host,
                f'await tools.run_agent.batch({{a:{{instruction:{instruction}}},b:{{instruction:"b"}}}});',
                invoke, {'run_agent':{}, 'read_file':{}}))
            assert page.state == 'failed'
            assert not calls
        finally:
            await host.aclose()
    asyncio.run(run())


def test_batch_peer_objects_are_deeply_frozen_even_in_nonstrict_callbacks():
    async def run():
        host = CodeModeHost()
        calls = []
        async def invoke(call):
            calls.append(call)
            return CodeModeToolResult(value={})
        try:
            page, _ = await _finish(host, await _start(host, '''
              await tools.run_agent.batch({
                a:{instruction:peers=>{
                  if(!Object.isFrozen(peers)||!Object.isFrozen(peers.b.work)) throw Error('not frozen');
                  const before=peers.b.work.work_id;
                  peers.b.work.work_id='changed';
                  if(peers.b.work.work_id!==before)throw Error('identity was mutable');
                  return before;
                }}, b:{instruction:'b'}
              });
            ''', invoke, {'run_agent':{}}))
            assert page.state == 'completed', page.error
            assert len(calls) == 1
            a,b = calls[0].arguments['members']
            assert a['parameters']['instruction'] == b['work_id']
        finally:
            await host.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('source', [
    'const huge=new Array(100000000).fill(1);return huge.length;',
    'function recurse(){return recurse()+1}recurse();',
])
def test_real_sdk_heap_and_stack_limits_fail_the_cell(source):
    async def run():
        host = CodeModeHost()
        try:
            page, output = await _finish(host, await _start(host, source))
            assert page.state == 'failed'
            assert page.error
            assert not any(record['kind']=='return' for record in output)
            assert ('memory' in page.error['message'] or 'stack' in page.error['message'])
        finally:
            await host.aclose()
    asyncio.run(run())


def test_external_watchdog_can_terminate_noninterruptible_compile_segment():
    async def run():
        host = CodeModeHost(limits=replace(CodeModeLimits(), segment_ms=5, watchdog_ms=20))
        try:
            source = '0;' * 1900000
            page, _ = await _finish(host, await _start(host, source))
            assert page.state == 'failed'
            assert page.error.get('code') == 'watchdog', page.error
        finally:
            await host.aclose()
    asyncio.run(run())


def test_delivery_owner_commits_before_guest_promise_continuation():
    async def run():
        host = CodeModeHost()
        committed = False
        async def invoke(call):
            nonlocal committed
            if call.arguments['step'] == 2:
                assert committed
                return CodeModeToolResult(value='committed')
            async def delivered():
                nonlocal committed
                await asyncio.sleep(.03)
                committed = True
            return CodeModeToolResult(value={'full':'business value'}, delivery_succeeded=delivered)
        try:
            page, output = await _finish(host, await _start(host, '''
              await tools.read_file({step:1});
              console.log(await tools.read_file({step:2}));
            ''', invoke))
            assert page.state == 'completed', page.error
            assert output == [{'kind':'log','text':'committed'}]
        finally:
            await host.aclose()
    asyncio.run(run())


def test_parallel_calls_settle_by_completion_without_slow_call_blocking_fast_call():
    async def run():
        host = CodeModeHost()
        order = []
        async def invoke(call):
            name = call.arguments['name']
            await asyncio.sleep(.06 if name == 'slow' else .002)
            order.append(name)
            return CodeModeToolResult(value=name)
        try:
            page, output = await _finish(host, await _start(host, '''
              await Promise.all(['slow','fast'].map(name=>tools.read_file({name}).then(console.log)));
            ''', invoke))
            assert page.state == 'completed'
            assert order == ['fast','slow']
            assert [item['text'] for item in output] == ['fast','slow']
        finally:
            await host.aclose()
    asyncio.run(run())


def test_many_failed_batch_preparations_release_current_data_without_total_call_cap():
    async def run():
        host = CodeModeHost(limits=replace(CodeModeLimits(), value_bytes=8192, cell_bytes=32768, host_bytes=65536, control_bytes=12288, page_bytes=8192))
        calls = []
        async def invoke(call):
            calls.append(call)
            return CodeModeToolResult(value={})
        try:
            page, output = await _finish(host, await _start(host, '''
              for(let i=0;i<150;i++){
                try{await tools.run_agent.batch({a:{instruction:()=>{throw Error('cancel preparation')}}})}
                catch(e){}
              }
              return 'all preparations released';
            ''', invoke, {'run_agent':{}}))
            assert page.state == 'completed', page.error
            assert not calls
            assert output == [{'kind':'return','text':'all preparations released'}]
        finally:
            await host.aclose()
    asyncio.run(run())


def test_terminal_consumed_cells_release_host_slots_and_data():
    async def run():
        host = CodeModeHost(limits=replace(CodeModeLimits(), value_bytes=8192, cell_bytes=16384, host_bytes=32768, control_bytes=4096, page_bytes=2048))
        try:
            for _ in range(60):
                page, output = await _finish(host, await _start(host, 'return "ok";'))
                assert page.state == 'completed'
                assert output == [{'kind':'return','text':'ok'}]
                await asyncio.sleep(0)
            await asyncio.sleep(.02)
            assert host.snapshot_cell_index() == ()
        finally:
            await host.aclose()
    asyncio.run(run())


def test_default_five_second_computation_profile_does_not_kill_long_tool_wait():
    async def run():
        host = CodeModeHost()
        async def invoke(_):
            await asyncio.sleep(5.3)
            return CodeModeToolResult(value='external wait completed')
        try:
            looping = await _start(host, 'while(true){}')
            waiting = await _start(host, 'console.log(await tools.read_file({}));', invoke)
            (failed, _), (completed, output) = await asyncio.gather(
                _finish(host, looping), _finish(host, waiting))
            assert failed.state == 'failed'
            assert 'bound' in failed.error['message'] or 'interrupted' in failed.error['message']
            assert completed.state == 'completed', completed.error
            assert output == [{'kind':'log','text':'external wait completed'}]
        finally:
            await host.aclose()
    asyncio.run(run())


def test_unknown_admission_retains_same_complete_output_until_stop_confirmation():
    async def run():
        host = CodeModeHost()
        try:
            cell = await _start(host, 'console.log("x".repeat(60000));console.log("other");')
            await _vm_terminal(host, cell)
            first = await host.lease_output(cell)
            assert await host.lease_output(cell) is first
            await host.stop(cell)
            assert not host.output_delivery_allowed(first)
            assert await host.lease_output(cell) is first
            assert first.record['text'] == 'x' * 60000
            await host.acknowledge_output(first, accepted=False)
            assert cell not in host.pending_output_cells()
        finally:
            await host.aclose()
    asyncio.run(run())


def test_empty_completed_cell_is_collected_without_any_model_acknowledgement():
    async def run():
        host = CodeModeHost()
        try:
            cell = await _start(host, '')
            await _vm_terminal(host, cell)
            async with asyncio.timeout(5):
                while any(item['cell_id'] == cell for item in host.snapshot_cell_index()):
                    await asyncio.sleep(.001)
            assert not host.pending_output_cells()
            with pytest.raises(CodeModeError, match='unavailable'):
                await host.wait_state(cell, 0)
        finally:
            await host.aclose()
    asyncio.run(run())


def test_source_ipc_resource_rejection_does_not_create_or_leave_a_live_cell():
    async def run():
        host = CodeModeHost(limits=replace(CodeModeLimits(), value_bytes=8192,cell_bytes=16384,host_bytes=32768,control_bytes=4096,page_bytes=2048))
        try:
            # Source's literal bytes fit its limit; JSON escaping exceeds the
            # per-cell IPC allowance. Rejection must happen before live admission.
            with pytest.raises(CodeModeError, match='frame'):
                await _start(host, '\x00' * 8000)
            assert host.snapshot_cell_index() == ()
        finally:
            await host.aclose()
    asyncio.run(run())


def test_guest_prototype_edits_cannot_drop_invalid_tool_fields():
    async def run():
        host = CodeModeHost()
        calls = []
        async def invoke(call):
            calls.append(call)
            return CodeModeToolResult(value={})
        try:
            page, output = await _finish(host, await _start(host, '''
              Array.prototype[Symbol.iterator]=function*(){};
              Array.prototype.map=()=>[];
              Function.prototype.call=()=>0;
              try{await tools.read_file({invalid:undefined})}
              catch(e){console.log('strict bridge retained');return;}
              throw Error('invalid field silently dropped');
            ''', invoke))
            assert page.state == 'completed', page.error
            assert not calls
            assert output == [{'kind':'log','text':'strict bridge retained'}]
        finally:
            await host.aclose()
    asyncio.run(run())


def test_delivery_owner_failure_does_not_resolve_same_promise_twice():
    async def run():
        host = CodeModeHost()
        async def invoke(_):
            async def delivered():
                raise RuntimeError('actual observation owner failed')
            return CodeModeToolResult(value={'accepted':True}, result_id='actual-result', delivery_succeeded=delivered)
        try:
            page, output = await _finish(host, await _start(host, 'await tools.read_file({});console.log("must not continue");', invoke))
            assert page.state == 'failed'
            assert page.error['code'] == 'delivery_owner_error'
            assert page.error['result_id'] == 'actual-result'
            assert not output
        finally:
            await host.aclose()
    asyncio.run(run())


def test_return_tojson_tools_are_settled_and_logs_precede_the_single_return():
    async def run():
        host = CodeModeHost()
        async def invoke(_):
            await asyncio.sleep(.01)
            return CodeModeToolResult(value='serialization-side tool settled')
        try:
            page, output = await _finish(host, await _start(host, '''
              return {toJSON(){
                tools.read_file({}).then(value=>console.log(value));return {answer:42};
              }};
            ''', invoke))
            assert page.state == 'completed', page.error
            assert output == [{'kind':'log','text':'serialization-side tool settled'},
                              {'kind':'return','text':'{"answer":42}'}]
        finally:
            await host.aclose()
    asyncio.run(run())


def test_uncaught_error_diagnostic_getters_cannot_dispatch_more_tools():
    async def run():
        host = CodeModeHost()
        calls=[]
        async def invoke(call):
            calls.append(call)
            return CodeModeToolResult(value={})
        try:
            page,_ = await _finish(host,await _start(host, '''
              const error={get name(){tools.read_file({});return 'Error';},message:'failed'};
              throw error;
            ''',invoke))
            assert page.state == 'failed'
            assert not calls
        finally:
            await host.aclose()
    asyncio.run(run())


def test_catalog_signature_is_exact_json_metadata_not_a_guest_business_value():
    async def run():
        host = CodeModeHost()
        try:
            cell=await _start(host,'return "directory installed";',catalog={
                'read_file':{'parameters':{'type':'object','properties':{'x':{'maximum':2**64-1}}}}})
            page,output=await _finish(host,cell)
            assert page.state=='completed'
            assert output==[{'kind':'return','text':'directory installed'}]
        finally:
            await host.aclose()
    asyncio.run(run())


def test_host_close_cancels_waits_and_still_settles_an_existing_output_reservation():
    async def run():
        host = CodeModeHost()
        dispatched=asyncio.Event()
        async def invoke(call):
            dispatched.set()
            await call.cancelled.wait()
            return CodeModeToolResult(value={'actual':'settled on close'},result_id='close-result')
        cell=await _start(host,'console.log("accepted pending input");await tools.read_file({});',invoke)
        await dispatched.wait()
        observation=await host.lease_output(cell)
        await host.aclose()
        assert host.snapshot_cell_index()==()
        assert not host.output_delivery_allowed(observation)
        await host.acknowledge_output(observation, accepted=False)
        assert not host.pending_output_cells()
    asyncio.run(run())


def test_sidecar_process_loss_does_not_replay_and_keeps_actual_late_result():
    async def run():
        host=CodeModeHost()
        dispatched=asyncio.Event()
        calls=[]
        async def invoke(call):
            calls.append(call)
            dispatched.set()
            await call.cancelled.wait()
            return CodeModeToolResult(value={'effect':'actually settled'},result_id='sidecar-late-result')
        try:
            cell=await _start(host,'await tools.read_file({});console.log("cannot resume");',invoke)
            await dispatched.wait()
            host._process.kill()
            page,output=await _finish(host,cell)
            assert page.state=='failed'
            assert len(calls)==1
            assert len(output)==1 and output[0]['kind']=='tool_result'
            assert output[0]['result']=={'effect':'actually settled'}
            with pytest.raises(CodeModeError,match='sidecar'):
                await _start(host,'return "must not restart old VM";')
        finally:
            await host.aclose()
    asyncio.run(run())


def test_console_preserves_nul_and_lone_surrogates_with_valid_json_transport():
    async def run():
        host=CodeModeHost()
        try:
            page,output=await _finish(host,await _start(host,r'console.log("a\u0000b\ud800");return "\udc00";'))
            assert page.state=='completed',page.error
            assert output==[{'kind':'log','text':'a\0b\ud800'},{'kind':'return','text':'\udc00'}]
        finally:
            await host.aclose()
    asyncio.run(run())


def test_host_aggregate_current_data_limit_rejects_before_admission_and_releases():
    async def run():
        limits=replace(CodeModeLimits(),value_bytes=8192,cell_bytes=32768,host_bytes=49152,
                       control_bytes=12288,page_bytes=8192)
        host=CodeModeHost(limits=limits)
        calls=[]
        async def invoke(call):
            calls.append(call)
            await call.cancelled.wait()
            return CodeModeToolResult(value={'actual':'settled'},result_id='aggregate-result')
        try:
            cells=[]
            for _ in range(3):
                cells.append(await _start(host,'await tools.read_file({});',invoke))
            async with asyncio.timeout(5):
                while len(calls)<3:
                    await asyncio.sleep(.001)
            with pytest.raises(CodeModeError,match='boundary'):
                await _start(host,'await tools.read_file({});',invoke)
            assert len(host.snapshot_cell_index())==3
            assert len(calls)==3
            await host.stop(cells[0])
            await _finish(host,cells[0])
            await asyncio.sleep(.01)
            accepted=await _start(host,'await tools.read_file({});',invoke)
            assert any(item['cell_id']==accepted for item in host.snapshot_cell_index())
        finally:
            await host.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('argument',[r'{value:"\ud800"}',r'{"\udc00":"valid"}',
                                     r'{value:"\u0000"}',r'{"\u0000":"valid"}'])
def test_argument_utf8_boundary_rejects_lone_surrogate_values_and_keys_before_submission(argument):
    async def run():
        host=CodeModeHost()
        calls=[]
        async def invoke(call):
            calls.append(call)
            return CodeModeToolResult(value={})
        try:
            page,output=await _finish(host,await _start(host,f'await tools.read_file({argument});',invoke))
            assert page.state=='failed'
            assert 'UTF-8' in page.error['message']
            assert not calls
            assert not output
        finally:
            await host.aclose()
    asyncio.run(run())


def test_return_serialization_shares_the_previous_vm_computation_segment():
    async def run():
        host=CodeModeHost(limits=replace(CodeModeLimits(),segment_ms=80,watchdog_ms=300))
        try:
            page,output=await _finish(host,await _start(host, '''
              const initial=Date.now()+50;while(Date.now()<initial){}
              return {toJSON(){const end=Date.now()+50;while(Date.now()<end){};return 'too long';}};
            '''))
            assert page.state=='failed'
            assert not any(record['kind']=='return' for record in output)
        finally:
            await host.aclose()
    asyncio.run(run())


def test_external_delivery_callback_wait_does_not_leave_a_watchdog_active():
    async def run():
        host=CodeModeHost(limits=replace(CodeModeLimits(),segment_ms=80,watchdog_ms=150))
        async def invoke(_):
            async def delivered():await asyncio.sleep(.22)
            return CodeModeToolResult(value='delivered',delivery_succeeded=delivered)
        try:
            page,output=await _finish(host,await _start(host,'console.log(await tools.read_file({}));',invoke))
            assert page.state=='completed',page.error
            assert output==[{'kind':'log','text':'delivered'}]
        finally:
            await host.aclose()
    asyncio.run(run())


def test_state_wait_ignores_output_and_cannot_consume_independent_records():
    async def run():
        host = CodeModeHost()
        dispatched, release = asyncio.Event(), asyncio.Event()
        async def invoke(_):
            dispatched.set()
            await release.wait()
            return CodeModeToolResult(value={})
        try:
            cell = await _start(host, 'console.log("same");console.log("same");await tools.read_file({});', invoke)
            await dispatched.wait()
            waiter = asyncio.create_task(host.wait_state(cell, 20))
            first = await host.lease_output(cell)
            assert first.record_sequence == 1 and first.record == {"kind": "log", "text": "same"}
            state = await waiter
            assert state["state"] == "running" and "output" not in state and "error" not in state
            assert await host.lease_output(cell) is first
            await host.acknowledge_output(first)
            second = await host.lease_output(cell)
            assert second.record_sequence == 2 and second.record == first.record
            await host.acknowledge_output(second)
            release.set()
            await _finish(host, cell)
        finally:
            release.set()
            await host.aclose()
    asyncio.run(run())


def test_root_diagnostic_survives_cleanup_and_resource_fallout_and_frozen_lease():
    async def run():
        host = CodeModeHost()
        try:
            cell = await _start(host, 'throw new Error("specific root cause");')
            await _vm_terminal(host, cell)
            owner = host._test_cells[cell]
            host._record_error(owner, {"name": "WorkerError", "message": "Worker exited"})
            first = await host.lease_output(cell)
            assert first.record["error"]["message"] == "specific root cause"
            assert first.record["error"]["secondary_causes"]["WorkerError"]["message"] == "Worker exited"
            host._record_error(owner, {"name": "ResourceError", "code": "data_boundary", "message": "late result resource failure"})
            assert first.record["error"]["message"] == "specific root cause"
            assert "data_boundary" not in first.record["error"]["secondary_causes"]
            await host.acknowledge_output(first)
            second = await host.lease_output(cell)
            assert second.record_sequence > first.record_sequence
            assert second.record["error"]["message"] == "specific root cause"
            assert "data_boundary" in second.record["error"]["secondary_causes"]
            await host.acknowledge_output(second)
        finally:
            await host.aclose()
    asyncio.run(run())


def test_definite_output_rejection_is_a_local_readable_failure_not_an_ack_or_recursive_input():
    async def run():
        host = CodeModeHost()
        try:
            cell = await _start(host, 'console.log("cannot accept this");console.log("independent record");')
            await _vm_terminal(host, cell)
            rejected = await host.lease_output(cell)
            failure = await host.reject_output_delivery(rejected, CodeModeError("invalid_cell_content", "Specific queue content rejection"))
            assert failure["delivery"] == "rejected"
            assert failure["source_exec_attempt_id"] == "test-source-attempt"
            assert failure["record_sequence"] == rejected.record_sequence
            assert failure["error"]["message"] == "Specific queue content rejection"
            assert host.capture_cell(cell).state == "failed"
            assert host.capture_cell(cell).error["message"] == "Specific queue content rejection"
            remaining = await host.lease_output(cell)
            assert remaining.record == {"kind": "log", "text": "independent record"}
            assert remaining.record_sequence == rejected.record_sequence + 1
            await host.acknowledge_output(remaining)
            assert not host.pending_output_cells()
            async with asyncio.timeout(5):
                while any(item["cell_id"] == cell for item in host.snapshot_cell_index()):
                    await asyncio.sleep(.001)
            with pytest.raises(CodeModeError, match="unavailable"):
                host.capture_cell(cell)
            assert failure["error"]["code"] == "invalid_cell_content"
        finally:
            await host.aclose()
    asyncio.run(run())


def test_unknown_admission_does_not_dispose_or_replace_the_original_record():
    async def run():
        host = CodeModeHost()
        try:
            cell = await _start(host, 'console.log("original");console.log("next");')
            await _vm_terminal(host, cell)
            original = await host.lease_output(cell)
            # A repository UNKNOWN is deliberately not a rejection/ACK call.
            await asyncio.sleep(.01)
            assert await host.lease_output(cell) is original
            assert original.record_sequence == 1
            assert len(host._test_cells[cell].output) == 2
            assert host.capture_cell(cell).error is None
            await host.acknowledge_output(original)
            following = await host.lease_output(cell)
            assert following.record_sequence == 2
            await host.acknowledge_output(following)
        finally:
            await host.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("close_host", [False, True])
def test_scope_stop_marks_all_cells_before_first_async_join(close_host):
    async def run():
        host = CodeModeHost()
        calls = []
        async def invoke(call):
            calls.append(call)
            await call.cancelled.wait()
            return CodeModeToolResult(value={"actual": "settled after stop"}, result_id="stop-result")
        first = await _start(host, 'await tools.read_file({});console.log("late first");', invoke)
        second = await _start(host, 'await tools.read_file({});console.log("late second");', invoke)
        other = await _start(host, 'await tools.read_file({});', invoke, turn_id="independent-turn")
        async with asyncio.timeout(5):
            while len(calls) != 3:
                await asyncio.sleep(.001)
        entered, release = asyncio.Event(), asyncio.Event()
        original_stop = host._stop_cell
        async def delayed_join(cell):
            entered.set()
            await release.wait()
            return await original_stop(cell)
        host._stop_cell = delayed_join
        closing = asyncio.create_task(host.aclose() if close_host else host.stop_turn("test-turn"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert host._test_cells[first].explicitly_stopped
            assert host._test_cells[second].explicitly_stopped
            assert host._test_cells[second].cancelled.is_set()
            assert host._test_cells[other].explicitly_stopped is close_host
            await asyncio.sleep(.03)
            assert not host._test_cells[first].output
            assert not host._test_cells[second].output
            assert not host.pending_output_cells()
        finally:
            release.set()
            await asyncio.wait_for(closing, 5)
            host._stop_cell = original_stop
            await host.aclose()
    asyncio.run(run())


def test_stop_during_late_result_retention_cannot_resurrect_output():
    async def run():
        host = CodeModeHost()
        entered, release = asyncio.Event(), asyncio.Event()
        wakes = []
        host.set_output_ready_callback(lambda: wakes.append(True))
        async def retain(_, size):
            entered.set()
            await release.wait()
            return True
        try:
            identity = await _start(host, 'console.log("unaccepted record");')
            await _vm_terminal(host, identity)
            cell = host._test_cells[identity]
            host._retain_data = retain
            pending = SimpleNamespace(sequence=1, tool_name="read_file", charge=0)
            settlement = asyncio.create_task(host._late_result(cell, pending,
                CodeModeToolResult(value={"actual": "settled"}, result_id="late-result")))
            await asyncio.wait_for(entered.wait(), 5)
            host.mark_turn_stopped("test-turn")
            baseline = len(wakes)
            release.set()
            await asyncio.wait_for(settlement, 5)
            assert not cell.output
            assert len(wakes) == baseline
            assert pending.charge > 0  # Existing pending owner must release retained bytes.
        finally:
            release.set()
            await host.aclose()
    asyncio.run(run())
