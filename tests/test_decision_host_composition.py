"""Production Session assembly shares one Decision request owner."""

import asyncio
from dataclasses import replace
import json
from threading import Event

import pytest

from tests.test_round4_plan_host import _DetachedDraftModel, _open_test_session


pytestmark = pytest.mark.postgres


def test_session_shares_decision_service_before_sealing_and_joins_it_on_close(
    tmp_path, monkeypatch, isolated_migrated_postgres_database,
):
    async def run():
        core, session = await _open_test_session(
            tmp_path=tmp_path, model=_DetachedDraftModel(), monkeypatch=monkeypatch,
            postgres_dsn=isolated_migrated_postgres_database.runtime_dsn,
        )
        service = session._decision_service
        assert session._memory_tools._decision_service is service
        assert session._decision_tools._service is service
        assert session._decision_tools._max_value_bytes == session._code_mode.limits.value_bytes
        assert not service.physical_close_complete
        try:
            await core.close_session(session.host_session_id)
            assert service.physical_close_complete
            assert session.physical_close_complete
        finally:
            await core.shutdown()

    asyncio.run(run())


def test_real_exec_cell_freezes_settings_before_vm_queue_and_new_cell_reads_changes(
    tmp_path, monkeypatch, isolated_migrated_postgres_database,
):
    from pulsara_agent.identity import random_id
    from pulsara_agent.llm.input import PromptContent
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.retrieval.config import RetrievalConnection
    from pulsara_agent.tools.builtins.filesystem import ReadFileTool
    from tests.test_round4_plan_host import _tool_call, _text

    (tmp_path / "hold.txt").write_text("hold one VM", encoding="utf-8")
    entered, release = Event(), Event()
    original_read = ReadFileTool.execute

    def read_with_barrier(tool, call):
        entered.set()
        assert release.wait(20), "test did not release the VM's physical read"
        return original_read(tool, call)

    monkeypatch.setattr(ReadFileTool, "execute", read_with_barrier)

    async def run():
        outputs, output_ready = {}, asyncio.Event()
        session = None

        class Model(_DetachedDraftModel):
            async def stream(self, request):
                self.requests.append(request)
                index = len(self.requests)
                if index == 1:
                    events = (
                        *_tool_call("exec_cell", random_id("call"), {"code":
                            'await tools.read_file({target:{kind:"PATH",path:"hold.txt"}}); return "released";'}),
                        *_tool_call("exec_cell", random_id("call"), {"code":
                            'return {label:"old", available:tools.decide.available()};'}),
                    )
                elif index == 2:
                    assert await asyncio.to_thread(entered.wait, 5)
                    cells = session._code_mode.snapshot_cell_index()
                    assert len(cells) == 2 and sum(cell["state"] == "queued" for cell in cells) == 1
                    settings = session._decision_tools._settings
                    settings.value = replace(settings.value, memory_retrieval=replace(
                        settings.value.memory_retrieval, decision=None,
                    ))
                    events = _tool_call("exec_cell", random_id("call"), {"code":
                        'return {label:"new", available:tools.decide.available()};'})
                else:
                    release.set()
                    await asyncio.wait_for(output_ready.wait(), 10)
                    events = _text("SETTINGS_FROZEN")
                for event in events:
                    yield event

        model = Model()
        core, session = await _open_test_session(
            tmp_path=tmp_path, model=model, monkeypatch=monkeypatch,
            postgres_dsn=isolated_migrated_postgres_database.runtime_dsn,
        )
        session._code_mode.limits = replace(session._code_mode.limits, vm_slots=1)
        settings = session._decision_tools._settings
        settings.value = replace(settings.value, memory_retrieval=replace(
            settings.value.memory_retrieval,
            decision=RetrievalConnection("https://example.invalid/decisions", "decision", "openai_decisions", "none"),
        ))
        original_append = session._code_mode._append_output

        def observe_output(cell, output):
            original_append(cell, output)
            if output.value is not None and output.value.get("kind") == "return":
                try:
                    value = json.loads(output.value["text"])
                except (ValueError, TypeError):
                    return
                if isinstance(value, dict) and value.get("label") in {"old", "new"}:
                    outputs[value["label"]] = value["available"]
                    if len(outputs) == 2:
                        output_ready.set()

        monkeypatch.setattr(session._code_mode, "_append_output", observe_output)
        try:
            await asyncio.wait_for(session.run_turn(PromptContent.text("Check frozen Decision availability."),
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS), 20)
            assert outputs == {"old": True, "new": False}
            assert all(request.compiled_input.system_prompt == model.requests[0].compiled_input.system_prompt
                       for request in model.requests)
            assert all(request.compiled_input.tools == model.requests[0].compiled_input.tools
                       for request in model.requests)
        finally:
            release.set()
            await core.shutdown()

    asyncio.run(run())


def test_host_close_seals_decision_before_lock_wait_and_joins_active_sdk_request(
    tmp_path, monkeypatch, isolated_migrated_postgres_database,
):
    import httpx2
    from time import monotonic
    from pulsara_agent.conversation_kernel.decision_tools import decision_request
    from pulsara_agent.llm.adapters.openai import client as openai_client
    from pulsara_agent.process_credential_boundary import ProcessCredentialBoundHttpx2Client
    from pulsara_agent.retrieval.config import RetrievalConnection
    from pulsara_agent.retrieval.decision import DecisionError

    async def run():
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        sealed = asyncio.Event()
        sealing_tasks = []

        async def respond(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

        monkeypatch.setattr(openai_client, "ProcessCredentialBoundHttpx2Client", lambda **kwargs:
            ProcessCredentialBoundHttpx2Client(**kwargs, transport=httpx2.MockTransport(respond), trust_env=False))
        core, session = await _open_test_session(
            tmp_path=tmp_path, model=_DetachedDraftModel(), monkeypatch=monkeypatch,
            postgres_dsn=isolated_migrated_postgres_database.runtime_dsn,
        )
        service = session._decision_service
        selected = RetrievalConnection("https://provider.example/decisions", "configured", "openai_decisions", "none")
        batches = service.prepare_batches(selected, item_count=1,
            build_request=lambda start, end: decision_request("condition", ("text",), start, end))
        active = asyncio.create_task(service.evaluate(batches, deadline_monotonic=monotonic() + 10))
        closing = None
        original_stop = service.stop_admission

        def observe_stop():
            original_stop()
            sealing_tasks.append(asyncio.current_task())
            sealed.set()

        monkeypatch.setattr(service, "stop_admission", observe_stop)
        try:
            await asyncio.wait_for(entered.wait(), 5)
            async with session._lock:
                closing = asyncio.create_task(session.aclose())
                await asyncio.wait_for(sealed.wait(), 5)
                assert sealing_tasks[0] is closing
                with pytest.raises(DecisionError, match="closing"):
                    service.prepare_batches(selected, item_count=1,
                        build_request=lambda start, end: decision_request("condition", ("text",), start, end))
                await asyncio.wait_for(cleaning.wait(), 5)
                assert not active.done() and not closing.done()
                assert not service.physical_close_complete
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await active
            await asyncio.wait_for(closing, 10)
            assert service.physical_close_complete
            assert session.physical_close_complete
        finally:
            release.set()
            await asyncio.gather(active, *((closing,) if closing else ()), return_exceptions=True)
            await core.shutdown()

    asyncio.run(run())
