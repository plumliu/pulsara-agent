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
                        "code": 'console.log(await tools.run_agent({instruction:"Wait for the child release"}));',
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
