"""Dogfood observes accepted Cells before checking their admitted work."""
from __future__ import annotations

import asyncio
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.identity import random_id
from pulsara_agent.llm.adapters.openai.chat_completions import OpenAIChatCompletionsTransport
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.primitives.permission import PermissionMode
from tests.dogfood.run_content_revision_line_edit_dogfood import (
    _settle_code_mode_phase,
    _tool_trace,
)
from tests.support.round3 import CallbackScriptedKernelModel
from tests.test_round4_plan_host import _open_test_session, _text, _tool_call

pytestmark = pytest.mark.postgres


def test_phase_waits_for_actual_admission_output_and_root_idle_without_waiting_child(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        admission_entered, admission_release = asyncio.Event(), asyncio.Event()
        output_entered, output_release = asyncio.Event(), asyncio.Event()
        child_entered, child_release = asyncio.Event(), asyncio.Event()
        invoke = DirectKernelToolPort.invoke
        original_stream = OpenAIChatCompletionsTransport.stream
        root_id = None
        root_requests = []
        admissions = []

        async def mock_auxiliary_transport(adapter, *, call, context):
            # Background test-runtime calls are synthetic too; no HTTP is sent.
            adapter._mock_chunks = [
                {"choices": [{"index": 0, "delta": {"content": "DONE"}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ]
            async for event in original_stream(adapter, call=call, context=context):
                yield event

        async def delayed_admission(owner, **kwargs):
            if kwargs["tool_name"] == "run_agent":
                admissions.append(kwargs)
                admission_entered.set()
                await admission_release.wait()
            return await invoke(owner, **kwargs)

        async def stream(request):
            if request.session_id != root_id:
                child_entered.set()
                await child_release.wait()
                events = _text("CHILD_DONE")
            else:
                root_requests.append(request)
                if len(root_requests) == 1:
                    events = _tool_call("exec_cell", random_id("call"), {
                        "code": 'console.log(await tools.run_agent([{instruction:"Wait for the child release"}]));',
                    })
                elif len(root_requests) == 2:
                    events = _text("SOURCE_TURN_DONE_BEFORE_ADMISSION")
                else:
                    output_entered.set()
                    await output_release.wait()
                    events = _text("FORMAL_OUTPUT_OBSERVED")
            for event in events:
                yield event

        monkeypatch.setattr(OpenAIChatCompletionsTransport, "stream", mock_auxiliary_transport)
        monkeypatch.setattr(DirectKernelToolPort, "invoke", delayed_admission)
        model = CallbackScriptedKernelModel(stream)
        core, root = await _open_test_session(
            tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
            monkeypatch=monkeypatch,
        )
        root_id = root.session_id
        waiting = None
        try:
            async with asyncio.timeout(20):
                source = await root.run_turn(
                    PromptContent.text("Create exactly one held child. Do not use or save memory."),
                    command_id=random_id("command"),
                    requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                )
                assert source.final_text == "SOURCE_TURN_DONE_BEFORE_ADMISSION"
                await admission_entered.wait()
                waiting = asyncio.create_task(_settle_code_mode_phase(root, trace_start=0))
                await asyncio.sleep(0.02)
                assert not waiting.done()
                assert not child_entered.is_set()
                assert await root._io.run(
                    root.repository.list_root_agent_session_ids,
                    root_session_id=root_id, deadline_monotonic=monotonic() + 5,
                ) == ()
                admission_release.set()
                await child_entered.wait()
                await output_entered.wait()
                assert root.active_root_turn_id() is not None
                assert not waiting.done()
                output_release.set()
                outputs = await waiting
                assert len(outputs) == 1
                assert outputs[0]["status"] == "CONSUMED"
                assert outputs[0]["record"]["kind"] == "log"
                assert root.active_root_turn_id() is None
                assert not root._code_mode.snapshot_cell_index()
                assert len(admissions) == 1
                agents = await root._io.run(
                    root.repository.list_root_agent_session_ids,
                    root_session_id=root_id, deadline_monotonic=monotonic() + 5,
                )
                assert len(agents) == 1
                works = await root._io.run(
                    root.repository.list_agent_works, root_session_id=root_id,
                    agent_id=agents[0], limit=10, deadline_monotonic=monotonic() + 5,
                )
                assert len(works) == 1 and works[0]["status"] == "running"
                assert not child_release.is_set()
                trace = _tool_trace(root)
                assert [(row["tool_name"], row["source_kind"]) for row in trace] == [
                    ("exec_cell", "MODEL_TOOL_CALL"), ("run_agent", "CODE_MODE_CALL"),
                ]
        finally:
            admission_release.set()
            output_release.set()
            child_release.set()
            if waiting is not None and not waiting.done():
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
            await core.shutdown()
    asyncio.run(scenario())


def test_decision_dogfood_program_uses_current_guest_surface_without_network():
    import json

    from pulsara_agent.code_mode import CodeModeHost, CodeModeToolResult
    from tests.dogfood.run_code_mode_decide_dogfood import MATERIALS, CONDITION, _program
    from tests.test_code_mode_runtime import _finish, _start

    async def run():
        calls, writes = [], []
        material_by_path = {path: text for path, text, _ in MATERIALS}
        async def invoke(call):
            calls.append(call.tool_name)
            args = call.arguments
            if call.tool_name == 'query_agents':
                value = {'agents': [], 'next': None}
            elif call.tool_name == 'read_file':
                value = {'target': args['target'], 'content': material_by_path[args['target']['path']]}
            elif call.tool_name == 'decide':
                if not args['condition'].strip():
                    return CodeModeToolResult(state='FAILED', error_code='INVALID_ARGUMENTS',
                                              error_message='condition must be nonblank')
                assert args['condition'] == CONDITION
                value = {'decisions': [expected for _, _, expected in MATERIALS] if args['inputs'] else []}
            elif call.tool_name == 'write_file':
                writes.append(json.loads(args['content']))
                value = {'target': args['target']}
            else:
                raise AssertionError(f'unexpected network or delegation: {call.tool_name}')
            return CodeModeToolResult(value=value)

        host = CodeModeHost()
        try:
            catalog = {name: {} for name in ('run_agent', 'query_agents', 'send_agent_message',
                                             'wait_agent', 'stop_agent', 'read_file', 'write_file')}
            page, records = await _finish(host, await _start(host, _program(), invoke, catalog,
                                                            decision_available=True))
            assert page.state == 'completed', page.error
            returned = json.loads(next(item['text'] for item in records if item['kind'] == 'return'))
            assert 'helpers' not in returned
            assert returned['decisions'] == [expected for _, _, expected in MATERIALS]
            assert [item['source']['path'] for item in returned['selected']] == ['writing.txt', 'guidance.txt']
            assert writes == [{'condition': CONDITION, 'decisions': returned['decisions'],
                               'selected': returned['selected']}]
            assert calls == ['query_agents', *(['read_file'] * 4), *(['decide'] * 3), 'write_file']
        finally:
            await host.aclose()
    asyncio.run(run())


def test_subagent_evidence_saves_real_host_frozen_values_and_closed_sessions(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    import json
    from collections.abc import Mapping
    from contextlib import ExitStack
    from dataclasses import fields, is_dataclass

    from pulsara_agent.settings import LocalSettingsStore
    from tests.dogfood.run_stop_chain_dogfood import Evidence
    from tests.dogfood.run_subagent_array_dogfood import _observe_sessions, _save, _settle, _snapshot

    def has_frozen_mapping(value):
        if isinstance(value, Mapping):
            return not isinstance(value, dict) or any(has_frozen_mapping(v) for v in value.values())
        if isinstance(value, (list, tuple)):
            return any(has_frozen_mapping(v) for v in value)
        if is_dataclass(value) and not isinstance(value, type):
            return any(has_frozen_mapping(getattr(value, f.name)) for f in fields(value))
        return False

    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        destination = tmp_path / "evidence.json"
        evidence = Evidence(destination, LocalSettingsStore(tmp_path / "home/local-settings.yaml").read(), "https://api.deepseek.com")
        evidence.data["inputs"] = []
        original_stream = OpenAIChatCompletionsTransport.stream

        async def auxiliary(adapter, *, call, context):
            adapter._mock_chunks = [
                {"choices": [{"index": 0, "delta": {"content": "DONE"}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ]
            async for event in original_stream(adapter, call=call, context=context):
                yield event

        async def stream(request):
            evidence.data["inputs"].append(request.wire_input_plan.materialization)
            events = _tool_call("exec_cell", random_id("call"), {
                "code": 'console.log(await tools.run_agent([{work_title:"Probe", instruction:"Reply DONE"}]));',
            }) if len(evidence.data["inputs"]) == 1 else _text("DONE")
            for event in events:
                yield event

        monkeypatch.setattr(OpenAIChatCompletionsTransport, "stream", auxiliary)
        core, root = await _open_test_session(
            tmp_path=tmp_path, model=CallbackScriptedKernelModel(stream),
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch,
        )
        with ExitStack() as stack:
            sessions = _observe_sessions(stack, core)
            sessions.append(root)  # The helper opened ROOT before observation was installed.
            try:
                evidence.data["result"] = await root.run_turn(
                    PromptContent.text("Delegate one small work"), command_id=random_id("command"),
                    requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                )
                await _settle(root, core)
                evidence.data["snapshot"] = await asyncio.to_thread(_snapshot, root)
                assert has_frozen_mapping(evidence.data), "Exercise actual frozen Host values, not decoded JSON"
                await _save(evidence)
                saved = json.loads(destination.read_text())
                assert saved["snapshot"]["works"][0]["work_title"] == "Probe"
                assert len(saved["inputs"]) == len(evidence.data["inputs"])
                assert saved["result"]["final_text"] == "DONE"
                assert not destination.with_suffix(".writing").exists()
            finally:
                await core.shutdown()
                evidence.data["closed"] = [session.physical_close_complete for session in sessions]
                await _save(evidence)
            saved = json.loads(destination.read_text())
            assert len(saved["closed"]) == 2 and all(saved["closed"])
            assert saved["snapshot"]["cell_sources"]
    asyncio.run(scenario())
