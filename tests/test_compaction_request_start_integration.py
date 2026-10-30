"""Canonical request start and delivery across a real summary transport barrier."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from time import monotonic
from types import SimpleNamespace
from threading import Event

import pytest
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.compaction.contracts import ResolvedCompactionPolicy
from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.memory.contracts import MemoryUsePolicy
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent, text_part_values
from pulsara_agent.llm.provider_replay import ProviderAssistantReplayCodecKind
from pulsara_agent.ports.provider_stream import freeze_provider_adapter_completed_replay_payload
from pulsara_agent.primitives.context import canonical_json_bytes, freeze_json
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.dogfood.run_compaction_request_start_dogfood import _seed_history
from tests.support.model_config import test_model_binding, test_model_runtime
from tests.support.round3 import CallbackScriptedKernelModel
from tests.test_round4_plan_host import _open_test_session, _text, _tool_call
from tests.test_round9_2_hook_integration_postgres import _CompactionSummaryExecution


pytestmark = pytest.mark.postgres

_INITIAL = "CURRENT_REQUEST_EXACT_ORIGINAL"
_HUMAN = "HUMAN_STEER_AFTER_AGENT"
_AGENT = "AGENT_MESSAGE_BEFORE_HUMAN"
_COMPLETION = "INDEPENDENT_AGENT_COMPLETION_AFTER_HUMAN"
_SUMMARY = "Earlier investigation finished; continue the exact current request."


class _SummaryExecution(_CompactionSummaryExecution):
    def __init__(self, owner):
        super().__init__(_SUMMARY)
        self.owner = owner
        self.first = True

    async def read_next(self):
        if self.first:
            self.first = False
            self.owner.started.set()
            await self.owner.release.wait()
        return await super().read_next()

    async def request_cancel(self, *, reason):
        del reason
        await self.aclose()

    async def aclose(self):
        self.owner.release.set()
        await super().aclose()


class _SummaryTransport:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.contexts = []
        self.calls = []

    def open_stream(self, *, call, context):
        self.calls.append(call)
        self.contexts.append(context)
        return _SummaryExecution(self)


class _Model(CallbackScriptedKernelModel):
    def __init__(self):
        super().__init__(self._stream)
        self.root_id = None
        self.root_requests = []
        self.child_requests = []
        self.child_started = asyncio.Event()
        self.child_release = asyncio.Event()
        self.summary = _SummaryTransport()

    def resolve_compaction_summary_call(self, **kwargs):
        call = super().resolve_compaction_summary_call(**kwargs)
        self.summary.binding_id = call.target.transport.binding_id
        self.summary.contract_version = call.target.transport.contract_version
        call = replace(call, target=replace(call.target, transport=self.summary))
        self.model_runtime.register_test_call_override(call)
        return call

    async def _stream(self, request):
        if request.session_id != self.root_id:
            self.child_requests.append(request)
            self.child_started.set()
            await self.child_release.wait()
            events = _text(_COMPLETION)
        else:
            self.root_requests.append(request)
            if len(self.root_requests) == 1:
                events = _tool_call("run_agent", random_id("call"), {
                    "instruction": "Wait for the independent investigation to finish.",
                })
            elif len(self.root_requests) == 2:
                events = _text("PREDECESSOR_DETAILS " + "completed investigation evidence " * 6000)
            else:
                events = _text("CURRENT_FINISHED")
        for event in events:
            yield event


async def _eventually(predicate):
    async with asyncio.timeout(20):
        while not await predicate():
            await asyncio.sleep(0.01)


async def _wait_summary(model, root, command_id, pending):
    async with asyncio.timeout(20):
        while not model.summary.started.is_set():
            if pending is not None and pending.done():
                await pending
                raise AssertionError("request completed without opening its summary")
            result = await root.query_command(command_id)
            if result is not None and result.status == "REJECTED":
                raise AssertionError(f"request failed before summary: {result}")
            await asyncio.sleep(0.01)


def _rows(session, query, parameters=()):
    with session.repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        return connection.execute(query, parameters).fetchall()


def _render(messages):
    return "\n".join(text for message in messages for text in text_part_values(message.content))


async def _history(core, root, model):
    model.root_id = root.session_id
    root._compaction.policy = ResolvedCompactionPolicy(automatic_enabled=False)
    await root.run_turn(PromptContent.text("Start the independent source investigation."),
        command_id=random_id("command"), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
    await asyncio.wait_for(model.child_started.wait(), 10)
    children = await root._io.run(root.repository.list_root_agent_session_ids,
        root_session_id=root.session_id, deadline_monotonic=monotonic() + 10)
    assert len(children) == 1
    child_id = children[0]
    works = await root._io.run(root.repository.list_agent_works,
        root_session_id=root.session_id, agent_id=child_id, limit=10,
        deadline_monotonic=monotonic() + 10)
    assert len(works) == 1 and works[0]["status"] == "running"
    usage = await root.read_context_usage()
    ratio = float(usage["input_tokens"]) / int(usage["input_budget_tokens"])
    root._compaction.policy = replace(root._compaction.policy,
        automatic_enabled=True, auto_trigger_ratio=ratio * 0.95,
        post_compaction_target_ratio=ratio * 0.85, minimum_reclaim_tokens=1)
    return child_id, works[0]["work_id"]


@pytest.mark.parametrize("entrypoint", ["direct", "queued"])
def test_started_root_summary_preserves_exact_target_and_first_wire(
    tmp_path, monkeypatch, stage2_migrated_postgres_database, entrypoint,
):
    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        model = _Model()
        core, root = await _open_test_session(tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        pending = None
        try:
            child_id, work_id = await _history(core, root, model)
            baseline_opens = len(model.root_requests)
            command_id = random_id("command")
            if entrypoint == "direct":
                pending = asyncio.create_task(root.run_turn(PromptContent.text(_INITIAL),
                    command_id=command_id, requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS))
                queue_id = None
            else:
                accepted = await root.submit_prompt(command_id=command_id,
                    content=PromptContent.text(_INITIAL), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
                queue_id = accepted.prompt_delivery.queue_item_id
            await _wait_summary(model, root, command_id, pending)
            active = root.active_root_turn_id()
            assert active is not None
            turns = _rows(root, "SELECT * FROM pulsara_v3.turns WHERE session_id=%s AND status='RUNNING'", (root.session_id,))
            assert len(turns) == 1 and turns[0]["id"] == active
            entries = _rows(root, "SELECT id,input_origin,entry_sequence,inline_content FROM pulsara_v3.transcript_entries WHERE session_id=%s AND turn_id=%s ORDER BY entry_sequence", (root.session_id, active))
            assert len(entries) == 1
            assert entries[0]["id"] == turns[0]["initial_entry_id"]
            assert _INITIAL.encode() in bytes(entries[0]["inline_content"])
            revisions = _rows(root, "SELECT revision_ordinal FROM pulsara_v3.turn_context_binding_revisions WHERE session_id=%s AND turn_id=%s", (root.session_id, active))
            assert [r["revision_ordinal"] for r in revisions] == [0]
            query = await root.query_command(command_id)
            assert query.target_id == active and query.public_code == "TURN_RUNNING"
            if queue_id is not None:
                assert query.prompt_delivery.queue_status == "CONSUMED"
                assert query.prompt_delivery.execution.status == "running"
                assert query.prompt_delivery.consumed_entry_id == entries[0]["id"]
            assert len(model.root_requests) == baseline_opens
            assert _INITIAL not in _render(model.summary.contexts[0].messages)

            agent = await core._send_agent_message(root.session_id, _AGENT,
                SimpleNamespace(session_id=child_id, work_id=work_id))
            assert agent["delivery"] == "queued"
            q1 = await root.submit_prompt(command_id=random_id("command"),
                content=PromptContent.text("INDEPENDENT_FUTURE_REQUEST"))
            q2_command = random_id("command")
            q2 = await root.submit_prompt(command_id=q2_command, content=PromptContent.text(_HUMAN))
            converted = await root.steer_queued_prompt(command_id=random_id("command"),
                source_queue_item_id=q2.prompt_delivery.queue_item_id, target_turn_id=active)
            assert converted.status == "PENDING"
            assert (await root.query_command(q2_command)).prompt_delivery.execution.terminal_reason == "USER_REDIRECTED_TO_STEER"
            selected = await core._send_agent_message(root.session_id, _COMPLETION,
                SimpleNamespace(session_id=child_id, work_id=work_id))
            assert selected["delivery"] == "queued"
            model.child_release.set()

            async def selected_message_queued():
                return len(_rows(root, "SELECT id FROM pulsara_v3.prompt_queue_items WHERE session_id=%s AND target_turn_id=%s AND input_origin='AGENT_MESSAGE' AND source_work_id=%s AND status='PENDING'", (root.session_id, active, work_id))) == 2
            await _eventually(selected_message_queued)
            queued = _rows(root, "SELECT input_origin,source_session_id,source_work_id FROM pulsara_v3.prompt_queue_items WHERE session_id=%s AND target_turn_id=%s AND status='PENDING' ORDER BY queue_sequence", (root.session_id, active))
            assert [r["input_origin"] for r in queued] == ["AGENT_MESSAGE", "HUMAN_STEER", "AGENT_MESSAGE"]
            assert queued[0]["source_session_id"] == child_id
            assert queued[2]["source_work_id"] == work_id
            assert len(model.summary.calls) == 1 and len(model.root_requests) == baseline_opens
            assert _AGENT not in _render(model.summary.contexts[0].messages)
            assert _HUMAN not in _render(model.summary.contexts[0].messages)
            # Q1 remains independently queued; this test ends it explicitly so
            # only the first request's post-summary ordinary wire is observed.
            await root.cancel_queued_prompt(command_id=random_id("command"),
                source_queue_item_id=q1.prompt_delivery.queue_item_id)
            model.summary.release.set()
            if pending is not None:
                await asyncio.wait_for(pending, 20)
            else:
                async def finished():
                    return (await root.query_command(command_id)).public_code == "TURN_COMPLETED"
                await _eventually(finished)
            wire = model.root_requests[baseline_opens].compiled_input
            rendered = _render(wire.messages)
            assert rendered.index(_INITIAL) < rendered.index(_AGENT) < rendered.index(_HUMAN) < rendered.index(_COMPLETION)
            # Memory may independently recall the source Work. Count
            # exact delivered parts, rather than matching text inside recall JSON.
            parts = [text for message in wire.messages for text in text_part_values(message.content)]
            counts = {marker: parts.count(marker) for marker in (_INITIAL, _AGENT, _HUMAN, _COMPLETION)}
            assert all(count == 1 for count in counts.values()), counts
            consumed = _rows(root, "SELECT input_origin,entry_sequence FROM pulsara_v3.transcript_entries WHERE session_id=%s AND turn_id=%s ORDER BY entry_sequence", (root.session_id, active))
            assert [r["input_origin"] for r in consumed[:4]] == ["HUMAN_MESSAGE", "AGENT_MESSAGE", "HUMAN_STEER", "AGENT_MESSAGE"]
            assert len(model.summary.calls) == 1
        finally:
            model.child_release.set()
            model.summary.release.set()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            await core.shutdown()
    asyncio.run(scenario())


class _SimpleModel(_Model):
    def preflight_execution(self, request, **kwargs):
        delegate = super().preflight_execution(request, **kwargs)
        take_completion = delegate.take_completed_result_once

        def take_completed_result_once():
            completion = take_completion()
            return replace(completion, replay_payload=freeze_provider_adapter_completed_replay_payload(
                codec_kind=ProviderAssistantReplayCodecKind.RESPONSES_EXACT_OUTPUT_ITEMS,
                ordered_items=(freeze_json({"type": "message", "id": "msg_test_complete",
                    "role": "assistant", "status": "completed", "content": [
                        {"type": "output_text", "text": "FRESH_ROOT_FINISHED", "annotations": []}
                    ]}),)))

        delegate.take_completed_result_once = take_completed_result_once
        return delegate

    async def _stream(self, request):
        self.root_requests.append(request)
        for event in _text("FRESH_ROOT_FINISHED"):
            yield event


@pytest.mark.parametrize("memory_case", ["normal_steer", "initial_disable_no_steer", "human_disable_then_agent"])
def test_fresh_host_empty_epoch_adoption_delivers_steer_on_first_responses_wire(
    tmp_path, monkeypatch, stage2_migrated_postgres_database, memory_case,
):
    async def scenario():
        import pulsara_agent.conversation_kernel.host as kernel_host
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        model = _SimpleModel()
        runtime = test_model_runtime(model_id="test-pro", wire_api="openai_responses",
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn)
        model._model_runtime = runtime
        model._preparer = DirectKernelModelPort(model_runtime=runtime)
        monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
        monkeypatch.setattr(kernel_host.LocalMcpManagementService, "load_configs", lambda *_a, **_k: ())
        core = KernelHostCore.production(model_runtime=runtime)
        root = await core.open_session(HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path))
        try:
            await root.update_model_call_binding(test_model_binding(runtime))
            root._compaction.policy = ResolvedCompactionPolicy(automatic_enabled=False, minimum_reclaim_tokens=1)
            _seed_history(root)
            usage = await root.read_context_usage()
            ratio = usage["input_tokens"] / usage["input_budget_tokens"]
            root._compaction.policy = replace(root._compaction.policy,
                automatic_enabled=True, auto_trigger_ratio=ratio * 0.95,
                post_compaction_target_ratio=ratio * 0.85)
            command = random_id("command")
            original = _INITIAL + ("; don't use saved memory for this answer" if memory_case == "initial_disable_no_steer" else "")
            await root.submit_prompt(command_id=command, content=PromptContent.text(original))
            await _wait_summary(model, root, command, None)
            active = root.active_root_turn_id()
            assert active is not None and not model.root_requests
            revisions = _rows(root, "SELECT revision_ordinal FROM pulsara_v3.turn_context_binding_revisions WHERE session_id=%s AND turn_id=%s", (root.session_id, active))
            assert [r["revision_ordinal"] for r in revisions] == [0]
            if memory_case != "initial_disable_no_steer":
                supplement = _HUMAN + ("; don't use saved memory for this answer" if memory_case == "human_disable_then_agent" else "")
                accepted = await root.steer_active_turn(command_id=random_id("command"),
                    content=PromptContent.text(supplement), target_turn_id=active)
                assert accepted.status == "PENDING"
                if memory_case == "human_disable_then_agent":
                    delivery = await core._send_agent_message(root.session_id, _AGENT + "; use saved memory normally",
                        SimpleNamespace(session_id=root.session_id, work_id=None))
                    assert delivery["delivery"] == "queued"
            assert _INITIAL not in _render(model.summary.contexts[0].messages)
            model.summary.release.set()
            async def finished():
                result = await root.query_command(command)
                assert result.status != "REJECTED", result
                return result.public_code == "TURN_COMPLETED"
            await _eventually(finished)
            rendered = _render(model.root_requests[0].compiled_input.messages)
            if memory_case != "initial_disable_no_steer":
                assert rendered.index(_INITIAL) < rendered.index(_HUMAN)
            if memory_case != "normal_steer":
                assert model.root_requests[0].memory_context.memory_use_policy is MemoryUsePolicy.ALL_DISABLED_BY_USER
                assert '"source":"MEMORY_RECALL"' not in rendered
                assert '"source":"MEMORY_WRITE_HINT"' not in rendered
            assert len(model.summary.calls) == 1
        finally:
            model.summary.release.set()
            await core.shutdown()
    asyncio.run(scenario())


class _WorkModel(_Model):
    def __init__(self):
        super().__init__()
        self.agent_id = None
        self.first_work_id = None
        self.root_turn_calls = {}
        self.create_other = False

    async def _stream(self, request):
        if request.session_id != self.root_id:
            if self.agent_id is not None and request.session_id != self.agent_id:
                text = "OTHER_WORK_FINISHED"
            else:
                self.child_requests.append(request)
                text = "FIRST_WORK_FINAL" if len(self.child_requests) == 1 else "SECOND_WORK_FINISHED"
            events = _text(text)
        else:
            index = self.root_turn_calls.get(request.turn_id, 0)
            self.root_turn_calls[request.turn_id] = index + 1
            if index == 0:
                arguments = {"instruction": "OTHER_WORK" if self.create_other else
                    ("FIRST_WORK " + "independent evidence " * 1500) if self.agent_id is None else _INITIAL}
                if self.agent_id is not None and not self.create_other:
                    arguments.update(target={"kind": "AGENT", "agent_id": self.agent_id},
                        materials=[{"kind": "WORK", "work_id": self.first_work_id}])
                events = _tool_call("run_agent", random_id("call"), arguments)
            else:
                events = _text("ROOT_DELEGATED")
        for event in events:
            yield event


def test_subagent_first_summary_uses_full_initial_group_and_current_work(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        model = _WorkModel()
        core, root = await _open_test_session(tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        model.root_id = root.session_id
        try:
            await root.run_turn(PromptContent.text("Delegate the first Work."),
                command_id=random_id("command"), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            async def first_done():
                children = await root._io.run(root.repository.list_root_agent_session_ids,
                    root_session_id=root.session_id, deadline_monotonic=monotonic() + 10)
                if not children:
                    return False
                model.agent_id = children[0]
                works = await root._io.run(root.repository.list_agent_works,
                    root_session_id=root.session_id, agent_id=model.agent_id, limit=10,
                    deadline_monotonic=monotonic() + 10)
                model.first_work_id = works[0]["work_id"]
                return works[0]["status"] == "completed"
            await _eventually(first_done)
            child = core._live_agent_session(model.agent_id)
            async def child_idle():
                return child.active_root_turn_id() is None
            await _eventually(child_idle)
            usage = await child.read_context_usage()
            ratio = usage["input_tokens"] / usage["input_budget_tokens"]
            child._compaction.policy = replace(child._compaction.policy,
                automatic_enabled=True, auto_trigger_ratio=ratio * 0.95,
                post_compaction_target_ratio=ratio * 0.85, minimum_reclaim_tokens=1)
            await root.run_turn(PromptContent.text("Delegate the next Work with the prior final as material."),
                command_id=random_id("command"), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            await asyncio.wait_for(model.summary.started.wait(), 20)
            active = child.active_root_turn_id()
            assert active is not None and len(model.child_requests) == 1
            state = await child._io.run(child.repository.read_agent_runtime_state,
                session_id=child.session_id, deadline_monotonic=monotonic() + 10)
            current = await child._io.run(child.repository.query_agent_work,
                root_session_id=root.session_id, work_id=state["current_work_id"],
                deadline_monotonic=monotonic() + 10)
            assert current["status"] == "running" and current["turn_id"] == active
            entries = _rows(child, "SELECT id,input_origin,entry_sequence FROM pulsara_v3.transcript_entries WHERE session_id=%s AND turn_id=%s ORDER BY entry_sequence", (child.session_id, active))
            assert [r["input_origin"] for r in entries] == ["AGENT_INSTRUCTION", "AGENT_MATERIAL"]
            initial = _rows(child, "SELECT initial_entry_id FROM pulsara_v3.turns WHERE session_id=%s AND id=%s", (child.session_id, active))[0]["initial_entry_id"]
            assert initial == entries[0]["id"]
            queue = _rows(child, "SELECT status,consumed_entry_id FROM pulsara_v3.prompt_queue_items WHERE session_id=%s AND id=%s", (child.session_id, current["work_id"]))[0]
            assert queue == {"status": "CONSUMED", "consumed_entry_id": initial}
            assert _INITIAL not in _render(model.summary.contexts[0].messages)
            model.create_other = True
            await root.run_turn(PromptContent.text('Create an independent Agent during the summary.'),
                command_id=random_id('command'), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            ids = await root._io.run(root.repository.list_root_agent_session_ids,
                root_session_id=root.session_id, deadline_monotonic=monotonic()+10)
            other, = (identity for identity in ids if identity != child.session_id)
            outcome = await core._send_agent_message(child.session_id, f'{_AGENT}: {other}',
                SimpleNamespace(session_id=root.session_id, work_id=None))
            assert outcome["delivery"] == "queued"
            visible = await child._io.run(child.repository.list_query_agent_sessions,
                root_session_id=root.session_id, observer_session_id=child.session_id,
                limit=20, deadline_monotonic=monotonic()+10)
            assert {row['agent_id'] for row in visible} == {child.session_id, other}
            model.summary.release.set()
            async def second_done():
                row = await child._io.run(child.repository.query_agent_work,
                    root_session_id=root.session_id, work_id=current["work_id"],
                    deadline_monotonic=monotonic() + 10)
                assert row["status"] != "stopped", row
                return row["status"] == "completed"
            await _eventually(second_done)
            revisions = _rows(child, "SELECT revision_ordinal,source_through_sequence FROM pulsara_v3.turn_context_binding_revisions WHERE session_id=%s AND turn_id=%s ORDER BY revision_ordinal", (child.session_id, active))
            assert [revision["revision_ordinal"] for revision in revisions] == [0, 1]
            assert revisions[1]["source_through_sequence"] < entries[0]["entry_sequence"]
            rendered = _render(model.child_requests[1].compiled_input.messages)
            assert rendered.index(_INITIAL) < rendered.index(_AGENT)
            assert "FIRST_WORK_FINAL" in rendered
            assert len(model.summary.calls) == 1
            first = model.child_requests[0].compiled_input
            assert model.summary.contexts[0].system_prompt == first.system_prompt
            assert tuple(canonical_json_bytes({'name': tool.name, 'description': tool.description,
                                              'parameters': tool.parameters})
                         for tool in model.summary.contexts[0].tools) == tuple(tool.canonical_bytes for tool in first.tools)
            visible = await child._io.run(child.repository.list_query_agent_sessions,
                root_session_id=root.session_id, observer_session_id=child.session_id,
                limit=20, deadline_monotonic=monotonic()+10)
            assert {row['agent_id'] for row in visible} == {child.session_id, other}
        finally:
            model.summary.release.set()
            await core.shutdown()
    asyncio.run(scenario())


async def _stop(root, turn_id):
    return await root.request_stop_turn(command_id=random_id("command"),
        admission_deadline_ms=root.control_admission_deadline_ms(),
        expected_session_id=root.session_id, expected_host_session_id=root.host_session_id,
        target_turn_id=turn_id)


@pytest.mark.parametrize("lose_ack", [False, True])
def test_full_adoption_stop_drains_exact_database_winner_and_unsent_resources(
    tmp_path, monkeypatch, stage2_migrated_postgres_database, lose_ack,
):
    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        model = _Model()
        core, root = await _open_test_session(tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        written, release = Event(), Event()
        original_adopt = root.repository.adopt_context_snapshot
        writes = []

        def blocked_adoption(*args, **kwargs):
            result = original_adopt(*args, **kwargs)
            writes.append(result)
            written.set()
            if not release.wait(20):
                raise TimeoutError("test adoption acknowledgement barrier was not released")
            if lose_ack:
                raise OSError("injected lost FULL adoption acknowledgement")
            return result

        try:
            await _history(core, root, model)
            baseline_opens = len(model.root_requests)
            monkeypatch.setattr(root.repository, "adopt_context_snapshot", blocked_adoption)
            command = random_id("command")
            await root.submit_prompt(command_id=command, content=PromptContent.text(_INITIAL))
            await _wait_summary(model, root, command, None)
            active = root.active_root_turn_id()
            steer_command = random_id("command")
            await root.steer_active_turn(command_id=steer_command,
                content=PromptContent.text(_HUMAN), target_turn_id=active)
            model.summary.release.set()
            assert await asyncio.to_thread(written.wait, 20)
            adopted = _rows(root, "SELECT id,current_context_binding_revision_id,status FROM pulsara_v3.turns WHERE session_id=%s AND id=%s", (root.session_id, active))[0]
            revisions = _rows(root, "SELECT id,revision_ordinal,context_snapshot_id FROM pulsara_v3.turn_context_binding_revisions WHERE session_id=%s AND turn_id=%s ORDER BY revision_ordinal", (root.session_id, active))
            assert [r["revision_ordinal"] for r in revisions] == [0, 1]
            assert adopted["status"] == "RUNNING" and revisions[1]["context_snapshot_id"]
            assert adopted["current_context_binding_revision_id"] == revisions[1]["id"]
            assert len(model.root_requests) == baseline_opens
            first = await _stop(root, active)
            second = await _stop(root, active)
            assert first.status == second.status == "PENDING"
            await asyncio.sleep(0)
            release.set()

            async def stopped():
                result = await root.query_command(command)
                return result is not None and result.public_code == "USER_STOPPED" and root.active_root_turn_id() is None
            await _eventually(stopped)
            terminal = _rows(root, "SELECT status,terminal_reason,current_context_binding_revision_id FROM pulsara_v3.turns WHERE session_id=%s AND id=%s", (root.session_id, active))[0]
            assert terminal["status"] == "INTERRUPTED" and terminal["terminal_reason"] == "USER_STOPPED"
            assert terminal["current_context_binding_revision_id"] == revisions[1]["id"]
            assert len(writes) == 1 and len(model.summary.calls) == 1
            assert len(model.root_requests) == baseline_opens
            assert (await root.query_command(steer_command)).status == "REJECTED"
            assert not _rows(root, "SELECT id FROM pulsara_v3.prompt_queue_items WHERE session_id=%s AND target_turn_id=%s AND status='PENDING'", (root.session_id, active))
            assert len(_rows(root, "SELECT event_sequence FROM pulsara_v3.agent_events WHERE session_id=%s AND event_type='CompactionAdopted'", (root.session_id,))) == 1
            model.model_runtime.assert_no_provider_borrows(session_id=root.session_id, turn_id=active)
        finally:
            release.set()
            model.child_release.set()
            model.summary.release.set()
            await core.shutdown()
    asyncio.run(scenario())


def test_stopped_summary_request_does_not_replay_and_next_request_summarizes_again(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        model = _Model()
        core, root = await _open_test_session(tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        try:
            child_id, work_id = await _history(core, root, model)
            baseline_opens = len(model.root_requests)
            command1, command2, steer_command = (random_id("command") for _ in range(3))
            await root.submit_prompt(command_id=command1, content=PromptContent.text("R1_EXACT_REQUEST"))
            await _wait_summary(model, root, command1, None)
            first_turn = root.active_root_turn_id()
            await root.steer_active_turn(command_id=steer_command,
                content=PromptContent.text("R1_UNCONSUMED_STEER"), target_turn_id=first_turn)
            await root.submit_prompt(command_id=command2, content=PromptContent.text("R2_EXACT_REQUEST"))
            assert (await _stop(root, first_turn)).status == "PENDING"

            async def second_done():
                outcome = await root.query_command(command2)
                assert outcome is not None and outcome.status != "REJECTED", outcome
                return outcome.public_code == "TURN_COMPLETED"
            await _eventually(second_done)
            assert (await root.query_command(command1)).public_code == "USER_STOPPED"
            assert (await root.query_command(steer_command)).status == "REJECTED"
            assert len(model.summary.calls) == 2
            assert len(model.root_requests) == baseline_opens + 1
            next_request = model.root_requests[-1]
            assert next_request.turn_id != first_turn
            assert "R2_EXACT_REQUEST" in _render(next_request.compiled_input.messages)
            assert "R1_UNCONSUMED_STEER" not in _render(next_request.compiled_input.messages)
            child = await root._io.run(root.repository.query_agent_work,
                root_session_id=root.session_id, work_id=work_id, deadline_monotonic=monotonic() + 10)
            assert child["agent_id"] == child_id and child["status"] == "running"
        finally:
            model.child_release.set()
            model.summary.release.set()
            await core.shutdown()
    asyncio.run(scenario())
