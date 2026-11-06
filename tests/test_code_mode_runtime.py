"""Real fixed QuickJS SDK integration: cells never use Python/Node eval fallbacks."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
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
                            tool_catalog={"decide": {}, **(catalog or {"read_file": {}})}, invoke=invoke or empty,
                            decision_available=kwargs.pop("decision_available", False),
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


def test_decide_availability_is_local_sync_and_does_not_block_other_tools():
    async def run():
        calls = []
        async def invoke(call):
            calls.append(call.tool_name)
            return CodeModeToolResult(value={"text": "existing material"})
        host = CodeModeHost()
        try:
            page, records = await _finish(host, await _start(host, '''
              const available = tools.decide.available();
              if (typeof available !== 'boolean' || available !== false)
                throw new Error('availability must be the local boolean');
              const material = await tools.read_file({});
              return {available, material};
            ''', invoke, catalog={"decide": {}, "read_file": {}}))
            assert page.state == "completed", page.error
            assert calls == ["read_file"]
            assert json.loads(records[-1]["text"]) == {
                "available": False, "material": {"text": "existing material"},
            }
        finally:
            await host.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("available", [False, True])
def test_decide_available_function_and_captured_value_are_immutable_in_real_guest(available):
    async def run():
        calls = []
        async def invoke(call):
            calls.append(call)
            return CodeModeToolResult(value={})
        host = CodeModeHost()
        try:
            page, records = await _finish(host, await _start(host, '''
              const decide = tools.decide, available = decide.available;
              const original = available();
              const changed = [
                Reflect.set(tools, 'decide', () => null),
                Reflect.set(decide, 'available', () => !original),
                Reflect.deleteProperty(decide, 'available'),
                Reflect.set(available, 'value', !original),
              ];
              for (const mutate of [
                () => Object.defineProperty(decide, 'available', {value: () => !original}),
                () => Object.setPrototypeOf(decide, {}),
                () => Object.setPrototypeOf(available, {}),
              ]) {
                try { mutate(); throw Error('mutation succeeded'); }
                catch (error) { if (!(error instanceof TypeError)) throw error; }
              }
              globalThis.decisionAvailable = !original;
              for (let i = 0; i < 100; i++) {
                if (tools.decide.available() !== original) throw Error('value changed');
              }
              return {value: tools.decide.available(), type: typeof available(),
                same: tools.decide === decide && decide.available === available,
                frozen: [Object.isFrozen(tools), Object.isFrozen(decide), Object.isFrozen(available)],
                changed};
            ''', invoke, decision_available=available))
            assert page.state == "completed", page.error
            assert calls == []
            assert json.loads(records[-1]["text"]) == {
                "value": available, "type": "boolean", "same": True,
                "frozen": [True, True, True], "changed": [False, False, False, False],
            }
        finally:
            await host.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("initial", [False, True])
def test_queued_cell_keeps_admission_availability_and_new_cell_captures_new_value(initial):
    async def run():
        host = CodeModeHost()
        release = asyncio.Event()
        all_running = asyncio.Event()
        calls = []
        async def invoke(call):
            calls.append(call.tool_name)
            if len(calls) == host.limits.vm_slots:
                all_running.set()
            await release.wait()
            return CodeModeToolResult(value={})
        try:
            # Occupy all existing VM slots with real waiting Guests, then change
            # the caller's configuration fact after the next Cell is admitted.
            occupied = [await _start(host, 'await tools.read_file({});', invoke)
                        for _ in range(host.limits.vm_slots)]
            await asyncio.wait_for(all_running.wait(), 5)
            configured = initial
            queued = await _start(host, 'return tools.decide.available();', invoke,
                                  decision_available=configured)
            assert (await host.wait_state(queued, 0))["state"] == "queued"
            configured = not configured
            new_cell = await _start(host, 'return tools.decide.available();', invoke,
                                   decision_available=configured)
            assert (await host.wait_state(new_cell, 0))["state"] == "queued"
            release.set()
            for identity in occupied:
                assert (await _finish(host, identity))[0].state == "completed"
            for identity, expected in ((queued, initial), (new_cell, not initial)):
                page, records = await _finish(host, identity)
                assert page.state == "completed", page.error
                assert json.loads(records[-1]["text"]) is expected
            assert calls == ["read_file"] * host.limits.vm_slots
        finally:
            release.set()
            await host.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("availability", [None, 0, 1, "false", "true", {}, []])
def test_cell_admission_requires_a_strict_availability_boolean_before_allocation(availability, monkeypatch):
    import pulsara_agent.code_mode.host as host_module
    def unexpected_id(_kind):
        raise AssertionError("invalid admission must not allocate an identity")
    monkeypatch.setattr(host_module, "random_id", unexpected_id)
    async def run():
        host = CodeModeHost()
        try:
            with pytest.raises(CodeModeError, match="must be a boolean") as caught:
                await _start(host, "return 1;", decision_available=availability)
            assert caught.value.code == "invalid_availability"
            assert host._process is None and not host._cells
        finally:
            await host.aclose()
    asyncio.run(run())


def test_cell_admission_has_no_missing_availability_or_decide_catalog_fallback():
    async def run():
        host = CodeModeHost()
        async def invoke(_):
            raise AssertionError("invalid admission must not invoke a tool")
        arguments = dict(source="return 1;", source_exec_attempt_id="source", turn_id="turn",
                         snapshot=object(), tool_catalog={"read_file": {}}, invoke=invoke)
        try:
            with pytest.raises(TypeError, match="decision_available"):
                await host.start(**arguments)
            with pytest.raises(CodeModeError, match="directory must include decide"):
                await host.start(**arguments, decision_available=False)
            assert host._process is None and not host._cells
        finally:
            await host.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("availability", [{}, {"decision_available": None}, {"decision_available": 1},
                                         {"decision_available": "false"}])
def test_real_sidecar_rejects_missing_or_non_boolean_availability(availability):
    from pulsara_agent.identity import random_id
    async def run():
        host = CodeModeHost()
        process = await asyncio.create_subprocess_exec(str(host._node), str(host._script),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        frames = [{"type": "configure", "limits": asdict(host.limits)},
                  {"type": "start", "cell_id": random_id("cell"), "source": "return 1;", "tool_names": ["decide"], **availability}]
        stdout, stderr = await asyncio.wait_for(process.communicate(
            ("\n".join(json.dumps(frame) for frame in frames) + "\n").encode()), 5)
        assert process.returncode != 0
        assert b"Decision availability must be a boolean" in stderr
        assert [json.loads(line)["type"] for line in stdout.splitlines()] == ["configured"]
    asyncio.run(run())


async def _vm_terminal(host, cell):
    owned = host._test_cells[cell]
    async with asyncio.timeout(15):
        while owned.state not in {"completed", "failed", "terminated"}:
            await asyncio.sleep(.001)


def test_close_joins_a_real_sidecar_spawn_before_handle_publication(monkeypatch):
    async def run():
        host = CodeModeHost()
        spawned, release, closing = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_spawn = asyncio.create_subprocess_exec
        original_mark = host.mark_all_stopped
        processes = []

        async def spawn(*args, **kwargs):
            process = await original_spawn(*args, **kwargs)
            processes.append(process)
            spawned.set()
            await release.wait()
            return process

        def mark():
            original_mark()
            closing.set()

        monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
        monkeypatch.setattr(host, 'mark_all_stopped', mark)
        startup = asyncio.create_task(host._ensure_started())
        await spawned.wait()
        close = asyncio.create_task(host.aclose())
        await closing.wait()
        assert processes[0].returncode is None
        assert not close.done()
        assert not host.local_children_stopped and not host.physical_close_complete
        release.set()
        with pytest.raises(CodeModeError, match='closed during startup'):
            await startup
        await close
        assert processes[0].returncode is not None
        assert host.local_children_stopped and host.physical_close_complete

    asyncio.run(run())


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


def test_worker_request_ids_preserve_batch_preparation_resume_and_concurrent_results():
    from pulsara_agent.identity import is_valid_id

    async def run():
        host = CodeModeHost()
        requests, replies, calls = [], [], []
        prepared_reply, resume_preparation = asyncio.Event(), asyncio.Event()
        original_accept, original_send = host._accept_request, host._send

        def accept(cell, message):
            requests.append(dict(message))
            original_accept(cell, message)

        async def send(message):
            if message['type'] == 'result':
                replies.append(dict(message))
            elif message['type'] == 'resume' and message.get('keep_preparation'):
                prepared_reply.set()
                await resume_preparation.wait()
            await original_send(message)

        async def invoke(call):
            calls.append(call)
            if call.tool_name == 'read_file':
                if call.arguments['value'] == 'slow':
                    await asyncio.sleep(.02)
                return CodeModeToolResult(value=call.arguments['value'])
            return CodeModeToolResult(value={'accepted': True})

        host._accept_request, host._send = accept, send
        try:
            cell_id = await _start(host, '''
              await Promise.all([
                tools.run_agent.batch({a:{instruction:'first batch'}}),
                tools.run_agent.batch({b:{instruction:'second batch'}})
              ]);
              console.log(await Promise.all([
                tools.read_file({value:'slow'}), tools.read_file({value:'fast'})
              ]));
            ''', invoke, {'run_agent': {}, 'read_file': {}})
            await asyncio.wait_for(prepared_reply.wait(), 5)
            assert not calls, 'Batch execution must wait for the original prepare resume'
            owned = host._test_cells[cell_id]
            assert owned.preparations
            prepare_ids = {m['request_id'] for m in requests if m['type'] == 'prepare'}
            first_reply = json.loads(replies[0]['json'])
            assert first_reply['request_id'] == replies[0]['request_id']
            assert first_reply['request_id'] in prepare_ids
            assert set(first_reply) == {'request_id', 'peers'}
            resume_preparation.set()
            page, output = await _finish(host, cell_id)
            assert page.state == 'completed', page.error
            assert output == [{'kind': 'log', 'text': '["slow","fast"]'}]
            assert len(requests) == 6  # two preparations and four actual tool calls
            ids = [m['request_id'] for m in requests]
            assert len(set(ids)) == len(ids)
            assert all(is_valid_id(value, 'request') for value in ids)
            prepares = {m['request_id']: m for m in requests if m['type'] == 'prepare'}
            actual = [m for m in requests if m['type'] == 'call']
            assert all('sequence' not in m for m in prepares.values())
            assert [m['sequence'] for m in actual] == ['1', '2', '3', '4']
            assert [call.sequence for call in calls] == [1, 2, 3, 4]
            batches = [m for m in actual if m['tool_name'] == 'run_agent.batch']
            assert {m['preparation'] for m in batches} == set(prepares)
            assert all(m['request_id'] != m['preparation'] for m in batches)
            by_request = {m['request_id']: json.loads(m['json']) for m in replies}
            for preparation in prepares:
                assert set(by_request[preparation]) == {'request_id', 'peers'}
                assert by_request[preparation]['request_id'] == preparation
            assert not owned.pending and not owned.preparations
            await asyncio.wait_for(owned.vm_released.wait(), 5)
            assert owned.vm_released.is_set()
        finally:
            resume_preparation.set()
            await host.aclose()

    asyncio.run(run())


@pytest.mark.parametrize('kind', ['call', 'prepare'])
@pytest.mark.parametrize('invalid', ['1', 'request:' + 'a' * 25 + 'b', 'cell:' + 'a' * 26])
def test_worker_request_boundary_rejects_legacy_and_wrong_kind_ids(kind, invalid):
    from pulsara_agent.code_mode.host import _Cell

    async def run():
        host = CodeModeHost()
        reader = asyncio.StreamReader()
        terminated, invoked = [], []
        host._process = SimpleNamespace(stdout=reader, returncode=None, terminate=lambda: terminated.append(True))
        admitted = asyncio.get_running_loop().create_future()
        admitted.set_result(None)
        cell = _Cell('cell', 'turn', 'attempt', object(), {},
                     lambda _: invoked.append('call'), lambda _: invoked.append('prepare'), admitted)
        cell.state = 'running'
        host._cells[cell.cell_id] = cell
        task = asyncio.create_task(host._read_messages())
        reader.feed_data((json.dumps({'type': kind, 'cell_id': cell.cell_id, 'request_id': invalid,
                                     'sequence': '1', 'tool_name': 'read_file',
                                     'arguments_json': '{}', 'charge': 128}) + '\n').encode())
        await asyncio.wait_for(task, 1)
        assert not invoked and not cell.pending and not cell.preparations
        assert cell.state == 'failed' and cell.terminal_event.is_set()
        assert 'INVALID_ID' in host._sidecar_failure
        assert terminated == [True]

    asyncio.run(run())


def test_worker_rejects_legacy_result_request_id_before_guest_continuation():
    async def run():
        host = CodeModeHost()
        original_send = host._send
        calls = []

        async def send(message):
            if message['type'] == 'result':
                message = {**message, 'request_id': '1'}
            await original_send(message)

        async def invoke(call):
            calls.append(call)
            return CodeModeToolResult(value={'accepted': True})

        host._send = send
        try:
            page, output = await _finish(host, await _start(
                host, 'await tools.read_file({});console.log("must not continue");', invoke))
            assert page.state == 'failed'
            assert 'Invalid Code Mode request identity' in page.error['message']
            assert len(calls) == 1
            # The malformed transport must not lose the tool's accepted effect.
            assert [item['kind'] for item in output] == ['tool_result']
            assert output[0]['result'] == {'accepted': True}
        finally:
            await host.aclose()

    asyncio.run(run())


def test_batch_does_not_accept_legacy_preparation_result_id_field():
    async def run():
        host = CodeModeHost()
        original_send = host._send
        calls = []

        async def send(message):
            if message['type'] == 'result':
                value = json.loads(message['json'])
                if 'peers' in value:
                    value['id'] = value.pop('request_id')
                    message = {**message, 'json': json.dumps(value)}
            await original_send(message)

        async def invoke(call):
            calls.append(call)
            return CodeModeToolResult(value={})

        host._send = send
        try:
            cell_id = await _start(host, '''
              await tools.run_agent.batch({a:{instruction:'must not dispatch'}});
              console.log('must not continue');
            ''', invoke, {'run_agent': {}})
            page, output = await _finish(host, cell_id)
            assert page.state == 'failed'
            assert not calls and not output
            assert not host._test_cells[cell_id].preparations
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


def test_external_watchdog_can_terminate_noninterruptible_compile_segment(tmp_path, monkeypatch):
    async def run():
        from pulsara_agent.code_mode import host as host_module

        host = CodeModeHost(limits=replace(CodeModeLimits(), segment_ms=1000, watchdog_ms=1500))
        production = host._script.parent
        sdk = production.parent.parent / '_vendor/code_mode/quickjs-wasi/dist/index.js'
        bootstrap = tmp_path / 'blocked-compile-worker.mjs'
        # Interpose only the SDK's actual cell compilation entry. The real
        # production worker and QuickJS initialization remain unchanged; this
        # operation cannot reach an SDK interrupt callback or return on its own.
        bootstrap.write_text(
            "import { parentPort } from 'node:worker_threads';\n"
            f"import {{ QuickJS }} from {json.dumps(sdk.resolve().as_uri())};\n"
            "const evaluate = QuickJS.prototype.evalCode;\n"
            "QuickJS.prototype.evalCode = function(source, filename, ...args) {\n"
            "  if (filename === 'cell.js') {\n"
            "    parentPort.postMessage({type:'test_compile_entered', filename});\n"
            "    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0);\n"
            "    throw new Error('Compilation barrier unexpectedly released');\n"
            "  }\n"
            "  return evaluate.call(this, source, filename, ...args);\n"
            "};\n"
            f"await import({json.dumps((production / 'worker.mjs').resolve().as_uri())});\n",
            encoding='utf-8',
        )
        sidecar = (production / 'sidecar.mjs').read_text(encoding='utf-8')
        worker_url = "new URL('./worker.mjs', import.meta.url)"
        assert sidecar.count(worker_url) == 1
        host._script = tmp_path / 'sidecar.mjs'
        host._script.write_text(sidecar.replace(worker_url,
            f"new URL({json.dumps(bootstrap.as_uri())})"), encoding='utf-8')
        entered, evidence = asyncio.Event(), []
        decode = host_module.json.loads

        def observed_decode(*args, **kwargs):
            value = decode(*args, **kwargs)
            if isinstance(value, dict) and value.get('type') == 'test_compile_entered':
                evidence.append(value)
                entered.set()
            return value

        monkeypatch.setattr(host_module.json, 'loads', observed_decode)
        try:
            cell_id = await _start(host, 'return 1;')
            await asyncio.wait_for(entered.wait(), 5)
            cell = host._test_cells[cell_id]
            assert evidence == [{'type': 'test_compile_entered', 'filename': 'cell.js', 'cell_id': cell_id}]
            assert not cell.terminal_event.is_set() and not cell.vm_released.is_set()
            page, output = await _finish(host, cell_id)
            assert page.state == 'failed'
            assert page.error.get('code') == 'watchdog', page.error
            assert not any(record['kind'] == 'return' for record in output)
            await asyncio.wait_for(cell.vm_released.wait(), 5)
            assert host._sidecar_failure is None and host._process.returncode is None
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


def test_oversized_accepted_value_is_catchable_and_reads_the_original_body_once():
    from pulsara_agent.identity import random_id
    from pulsara_agent.ports.tool_output import tool_output_read_call

    async def run():
        host = CodeModeHost(limits=replace(CodeModeLimits(), value_bytes=2048))
        session, entry = random_id("session"), random_id("entry")
        read = tool_output_read_call(session, entry)
        effects, deliveries, reads = [], [], []

        async def invoke(call):
            if "target" in call.arguments:
                reads.append(call.arguments)
                return CodeModeToolResult(value="exact frozen body")
            effects.append(call.sequence)
            async def delivered():
                deliveries.append("full-value-delivered")
            return CodeModeToolResult(value="x" * 8192, state="SUCCESS", result_id=random_id("result"),
                result_entry_id=entry, read=read, output_available=True,
                source_coverage="RETAINED_SNAPSHOT", source_coverage_reason="TERMINAL_RETENTION_GAP",
                required_facts={"exit_code":23}, delivery_succeeded=delivered)

        try:
            cell = await _start(host, '''
              try { await tools.read_file({effect:true}); }
              catch(e) {
                console.log(e.code,e.state,e.output_available,e.source_coverage,e.source_coverage_reason,e.required_facts.exit_code);
                console.log(await tools.read_file(e.read.arguments));
              }
            ''', invoke)
            page, output = await _finish(host, cell)
            assert page.state == "completed", page.error
            assert [record["text"] for record in output] == [
                "value_boundary SUCCESS true RETAINED_SNAPSHOT TERMINAL_RETENTION_GAP 23", "exact frozen body"]
            assert len(effects) == 1 and not deliveries
            assert reads == [read["arguments"]]
            assert not any(record["kind"] == "tool_result" for record in output)
        finally:
            await host.aclose()
    asyncio.run(run())


def test_cell_admission_budget_failure_precedes_vm_or_business_execution():
    from pulsara_agent.primitives.tool_result_budget import ToolResultPageBudget

    async def run():
        host = CodeModeHost()
        denied = ToolResultPageBudget("test-call", accepts_provider_body=lambda *_: False)
        try:
            with pytest.raises(CodeModeError, match="admission receipt"):
                await _start(host, "console.log('must not run');", page_budget=denied)
            assert not host._cells and host._process is None
        finally:
            await host.aclose()
    asyncio.run(run())


def test_uncaught_bridge_boundary_keeps_original_read_route_when_diagnostic_is_shortened():
    from pulsara_agent.identity import random_id
    from pulsara_agent.ports.tool_output import tool_output_read_call

    async def run():
        host = CodeModeHost(limits=replace(CodeModeLimits(), value_bytes=65536))
        read = tool_output_read_call(random_id("session"), random_id("entry"))
        async def invoke(_):
            return CodeModeToolResult(value="x" * 131072, result_id=random_id("result"),
                result_entry_id=read["arguments"]["target"]["entry_id"], read=read,
                output_available=True, required_facts={"details":"汉" * 2000})
        try:
            page, output = await _finish(host, await _start(host, "await tools.read_file({});", invoke))
            assert page.state == "failed" and not output
            assert page.error["code"] == "value_boundary" and page.error["state"] == "SUCCESS"
            assert page.error["read"] == read and page.error["output_available"]
            assert page.error["diagnostic_truncated"]
        finally:
            await host.aclose()
    asyncio.run(run())


def test_cell_stop_budget_failure_does_not_fence_the_running_script():
    from pulsara_agent.primitives.tool_result_budget import ToolResultPageBudget

    async def run():
        host = CodeModeHost()
        entered, release = asyncio.Event(), asyncio.Event()
        async def invoke(_):
            entered.set()
            await release.wait()
            return CodeModeToolResult(value={})
        try:
            cell = await _start(host, "await tools.read_file({});console.log('still running');", invoke)
            await entered.wait()
            denied = ToolResultPageBudget("test-call", accepts_provider_body=lambda *_: False)
            with pytest.raises(CodeModeError, match="stop receipt"):
                await host.stop(cell, page_budget=denied)
            assert not host._cells[cell].explicitly_stopped and not host._cells[cell].cancelled.is_set()
            release.set()
            page, output = await _finish(host, cell)
            assert page.state == "completed"
            assert output == [{"kind": "log", "text": "still running"}]
        finally:
            release.set()
            await host.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("refused_state", ["completed", "failed", "terminated"])
def test_cell_stop_quotes_each_possible_terminal_receipt_before_fencing(refused_state):
    from pulsara_agent.primitives.tool_result_budget import ToolResultPageBudget

    async def run():
        host = CodeModeHost()
        entered, release = asyncio.Event(), asyncio.Event()
        quoted = []
        async def invoke(_):
            entered.set()
            await release.wait()
            return CodeModeToolResult(value={})
        def quote(body, _):
            value = json.loads(body)
            quoted.append(value)
            return value["state"] != refused_state
        try:
            cell = await _start(host, "await tools.read_file({});", invoke)
            await entered.wait()
            with pytest.raises(CodeModeError, match="stop receipt"):
                await host.stop(cell, page_budget=ToolResultPageBudget("test-call", accepts_provider_body=quote))
            states = ["completed", "failed", "terminated"]
            assert [value["state"] for value in quoted] == states[:states.index(refused_state) + 1]
            assert all(value == {"target": {"kind": "CELL", "cell_id": cell}, "state": value["state"]}
                       for value in quoted)
            assert not host._cells[cell].explicitly_stopped
            release.set()
            page, _ = await _finish(host, cell)
            assert page.state == "completed"
        finally:
            release.set()
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


def test_retain_reservation_collision_redraws_and_concurrent_replies_match(monkeypatch):
    from pulsara_agent.code_mode import host as host_module
    from pulsara_agent.identity import derive_id

    async def run():
        host = CodeModeHost()
        host._process = SimpleNamespace(returncode=None)
        first = derive_id("request", "retain-test", "first")
        second = derive_id("request", "retain-test", "second")
        ids = iter((first, first, second))
        monkeypatch.setattr(host_module, "random_id", lambda kind: next(ids))
        sent = []
        async def send(message):
            sent.append(message)
        host._send = send
        cell = SimpleNamespace(cell_id="cell", reservations={})
        one = asyncio.create_task(host._retain_data(cell, 17))
        await asyncio.sleep(0)
        original_future = cell.reservations[first]
        two = asyncio.create_task(host._retain_data(cell, 31))
        await asyncio.sleep(0)
        assert cell.reservations[first] is original_future
        assert [(m["reservation"], m["bytes"]) for m in sent] == [(first, 17), (second, 31)]
        host._complete_retention(cell, {"reservation": second, "accepted": False})
        host._complete_retention(cell, {"reservation": first, "accepted": True})
        assert await one is True
        assert await two is False
        assert cell.reservations == {}

    asyncio.run(run())


@pytest.mark.parametrize("invalid", ["0123456789abcdef0123456789abcdef", "cell:aaaaaaaaaaaaaaaaaaaaaaaaaa"])
def test_retain_response_rejects_invalid_ids_and_settles_pending_on_protocol_failure(invalid):
    from pulsara_agent.code_mode.host import _Cell
    from pulsara_agent.identity import derive_id

    async def run():
        host = CodeModeHost()
        reader = asyncio.StreamReader()
        terminated = []
        host._process = SimpleNamespace(stdout=reader, returncode=None, terminate=lambda: terminated.append(True))
        admitted = asyncio.get_running_loop().create_future()
        admitted.set_result(None)
        cell = _Cell("cell", "turn", "attempt", object(), {}, lambda _: None, lambda _: None, admitted)
        cell.state = "running"
        host._cells[cell.cell_id] = cell
        known = derive_id("request", "retain-test", "known")
        unknown = derive_id("request", "retain-test", "unknown")
        waiting = asyncio.get_running_loop().create_future()
        cell.reservations[known] = waiting
        task = asyncio.create_task(host._read_messages())
        reader.feed_data((json.dumps({"type": "retained", "cell_id": cell.cell_id,
                                     "reservation": unknown, "accepted": True}) + "\n").encode())
        await asyncio.wait_for(cell.changed.wait(), 1)
        assert not waiting.done()
        assert cell.reservations == {known: waiting}
        reader.feed_data((json.dumps({"type": "retained", "cell_id": cell.cell_id,
                                     "reservation": invalid, "accepted": True}) + "\n").encode())
        await asyncio.wait_for(task, 1)
        assert waiting.result() is False
        assert cell.reservations == {}
        assert cell.state == "failed" and cell.terminal_event.is_set()
        assert "sidecar protocol failed" in host._sidecar_failure
        assert terminated == [True]

    asyncio.run(run())
