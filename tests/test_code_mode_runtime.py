"""Real fixed QuickJS SDK integration: cells never use Python/Node eval fallbacks."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from time import monotonic

import pytest

from pulsara_agent.code_mode import (
    CodeModeError, CodeModeHost, CodeModeLimits, CodeModeToolResult,
)


async def _start(host, source, invoke=None, catalog=None, **kwargs):
    async def empty(_):
        return CodeModeToolResult(value={})
    return await host.start(source=source, source_exec_attempt_id="test-source-attempt",
                            turn_id=kwargs.pop("turn_id", "test-turn"), snapshot=object(),
                            tool_catalog=catalog or {"read_file": {}}, invoke=invoke or empty, **kwargs)


async def _finish(host, cell, *, archive=None):
    records = []
    async with asyncio.timeout(15):
        while True:
            page = await host.observe(cell, 100, archive_output=archive)
            records.extend(page.output)
            await host.acknowledge(page)
            if page.state in {"completed", "failed", "terminated"} and not page.has_more and not page.pending_calls:
                return page, records


async def _vm_terminal(host, cell):
    async with asyncio.timeout(15):
        while True:
            item = next(item for item in host.snapshot_cell_index() if item['cell_id'] == cell)
            if item['state'] in {'completed','failed','terminated'}:
                return
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
            assert len(output) == 1 and output[0]["kind"] == "tool_result"
            assert output[0]["sequence"] == 1 and output[0]["result"] == {"actual": "settled"}
            assert output[0]["result_id"] == "accepted-result"
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
            page = await host.observe(second, 0)
            assert page.state == "queued"
            await host.acknowledge(page)
            assert (await host.stop(second))["state"] == "terminated"
            page, output = await _finish(host, second)
            assert not output and not page.error
            settle.set()
            assert (await _finish(host, first))[0].state == "completed"
        finally:
            settle.set()
            await host.aclose()
    asyncio.run(run())


def test_output_pages_are_complete_and_only_acknowledged_after_canonical_acceptance():
    async def run():
        host = CodeModeHost()
        try:
            cell = await _start(host, 'for(let i=0;i<5;i++)console.log(String(i)+"x".repeat(18000));')
            await _vm_terminal(host, cell)
            page = await host.observe(cell, 0)
            assert len(page.output) == 2 and page.has_more
            assert len(json.dumps(page.public_value()).encode()) <= 40000
            with pytest.raises(CodeModeError, match="Another reader"):
                await host.observe(cell, 0)
            host.abandon(page)
            repeated = await host.observe(cell, 0)
            assert repeated.output == page.output
            await host.acknowledge(repeated)
            _, output = await _finish(host, cell)
            assert [row["text"][0] for row in [*page.output, *output]] == list('01234')
        finally:
            await host.aclose()
    asyncio.run(run())


def test_large_output_uses_injected_existing_artifact_owner():
    async def run():
        host = CodeModeHost()
        archived = []
        async def archive(record):
            archived.append(record)
            return {"kind": record["kind"], "artifact": {"kind": "ARTIFACT", "artifact_id": "actual-artifact"},
                    "source_coverage": "COMPLETE"}
        try:
            page, output = await _finish(host, await _start(host, 'console.log("x".repeat(60000));'), archive=archive)
            assert page.state == "completed"
            assert len(archived) == 1 and len(archived[0]["text"]) == 60000
            assert output[0]["artifact"]["artifact_id"] == "actual-artifact"
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
            source = '0;' * 750000
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


def test_archived_output_is_reprepared_after_an_abandoned_canonical_observation():
    async def run():
        host = CodeModeHost()
        archived = []
        async def archive(record):
            assert len(record['text']) == 60000
            archived.append(record)
            return {'kind':'log','artifact':{'kind':'ARTIFACT','artifact_id':f'outer-result-{len(archived)}'}}
        try:
            cell = await _start(host, 'console.log("x".repeat(60000));')
            await _vm_terminal(host, cell)
            first = await host.observe(cell,0,archive_output=archive)
            host.abandon(first)
            second = await host.observe(cell,0,archive_output=archive)
            assert first.output[0]['artifact']['artifact_id'] == 'outer-result-1'
            assert second.output[0]['artifact']['artifact_id'] == 'outer-result-2'
            assert len(archived) == 2
            await host.acknowledge(second)
        finally:
            await host.aclose()
    asyncio.run(run())


def test_empty_completed_cell_keeps_terminal_observation_until_canonical_acceptance():
    async def run():
        host = CodeModeHost()
        try:
            cell = await _start(host, '')
            await _vm_terminal(host, cell)
            await asyncio.sleep(.03)
            page = await host.observe(cell,0)
            assert page.state == 'completed' and not page.output
            host.abandon(page)
            page = await host.observe(cell,0)
            assert page.state == 'completed'
            await host.acknowledge(page)
            await asyncio.sleep(.02)
            with pytest.raises(CodeModeError, match='unavailable'):
                await host.observe(cell,0)
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
        cell=await _start(host,'await tools.read_file({});',invoke)
        await dispatched.wait()
        observation=await host.observe(cell,0)
        await host.aclose()
        assert host.snapshot_cell_index()==()
        await host.acknowledge(observation)
        with pytest.raises(CodeModeError,match='closed'):
            await host.observe(cell,0)
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
