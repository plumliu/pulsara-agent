"""Captured request bindings survive settings edits across real Host boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from threading import Event

import pytest

from pulsara_agent.conversation_kernel.compaction.contracts import (
    ResolvedCompactionPolicy,
)
from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.identity import random_id
from pulsara_agent.llm.adapters.openai.chat_completions import (
    build_chat_completions_payload,
)
from pulsara_agent.llm.input import PromptContent, ToolSpec
from pulsara_agent.llm.model_catalog import (
    ReasoningEffortChoices,
    ReasoningSelectableControls,
)
from pulsara_agent.llm.model_connections import (
    ModelConnectionId,
    ReasoningEffortSelection,
)
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.primitives.context import thaw_json
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.dogfood.run_compaction_request_start_dogfood import _seed_history
from tests.support.model_config import (
    include_test_runtime_connections,
    test_model_binding,
    test_model_runtime,
)
from tests.test_compaction_request_start_integration import (
    _Model,
    _eventually,
    _render,
    _rows,
    _wait_summary,
)
from tests.test_round4_plan_host import _text, _tool_call

pytestmark = pytest.mark.postgres


class _CaptureModel(_Model):
    def __init__(self, runtime):
        super().__init__()
        self._model_runtime = runtime
        self._preparer = DirectKernelModelPort(model_runtime=runtime)
        self.hold_first = False
        self.ordinary_started = asyncio.Event()
        self.ordinary_release = asyncio.Event()
        self.delegate_work = False
        self.reused_agent = None

    def prepare_target_from_snapshot(self, request, *, resolution_snapshot):
        return self._preparer.prepare_target_from_snapshot(
            request, resolution_snapshot=resolution_snapshot
        )

    async def _stream(self, request):
        if request.session_id != self.root_id:
            self.child_requests.append(request)
            if len(self.child_requests) == 1:
                self.child_started.set()
                await self.child_release.wait()
            events = _text("WORK_FINISHED")
        else:
            self.root_requests.append(request)
            if self.hold_first and len(self.root_requests) == 1:
                self.ordinary_started.set()
                await self.ordinary_release.wait()
            if self.delegate_work and len(self.root_requests) in (1, 3):
                args = {
                    "instruction": "FIRST_WORK"
                    if len(self.root_requests) == 1
                    else "QUEUED_WORK"
                }
                if self.reused_agent is not None:
                    args["target"] = {"kind": "AGENT", "agent_id": self.reused_agent}
                events = _tool_call("run_agent", random_id("call"), args)
            else:
                events = _text("REQUEST_FINISHED")
        for event in events:
            yield event


def _request_payload(request):
    compiled, call = request.compiled_input, request.prepared_call.call
    return build_chat_completions_payload(
        call=call,
        context=LLMContext(
            messages=compiled.messages,
            context_id=compiled.context_id,
            resolved_model_call_id=call.resolved_model_call_id,
            model_call_index=request.model_call_index,
            tools=tuple(
                ToolSpec(item.name, item.description, thaw_json(item.parameters))
                for item in compiled.tools
            ),
            system_prompt=compiled.system_prompt,
            provider_wire_input_plan=request.wire_input_plan,
        ),
    )


async def _open(tmp_path, monkeypatch, dsn, *, second_connection=False):
    import pulsara_agent.conversation_kernel.host as kernel_host

    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    controls = ReasoningSelectableControls(
        effort=ReasoningEffortChoices(("low", "medium", "high"))
    )
    runtime = test_model_runtime(
        model_id="test-pro",
        wire_api="openai_chat_completions",
        reasoning=controls,
        postgres_dsn=dsn,
    )
    other = None
    if second_connection:
        other_runtime = test_model_runtime(
            model_id="test-other",
            wire_api="openai_chat_completions",
            reasoning=controls,
            connection_id=ModelConnectionId(random_id("connection")),
        )
        other = replace(
            test_model_binding(other_runtime),
            reasoning=ReasoningEffortSelection("high"),
        )
        include_test_runtime_connections(runtime, other_runtime)
    model = _CaptureModel(runtime)
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(
        kernel_host.LocalMcpManagementService, "load_configs", lambda *_a, **_k: ()
    )
    core = KernelHostCore.production(model_runtime=runtime)
    root = await core.open_session(
        HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path)
    )
    model.root_id = root.session_id
    high = replace(
        test_model_binding(runtime), reasoning=ReasoningEffortSelection("high")
    )
    await root.update_model_call_binding(high)
    root._compaction.policy = ResolvedCompactionPolicy(
        automatic_enabled=False, minimum_reclaim_tokens=1
    )
    return core, root, model, high, other


async def _complete(root, command):
    async def completed():
        result = await root.query_command(command)
        return result is not None and result.public_code == "TURN_COMPLETED"

    await _eventually(completed)


def test_active_request_settings_edit_keeps_summary_steer_and_first_ordinary_binding(
    tmp_path,
    monkeypatch,
    stage2_migrated_postgres_database,
):
    async def scenario():
        core, root, model, high, _ = await _open(
            tmp_path, monkeypatch, stage2_migrated_postgres_database.runtime_dsn
        )
        gate, release = asyncio.Event(), asyncio.Event()
        prepare = root._runner.compaction.prepare_precompile_admission

        async def gated(**kwargs):
            gate.set()
            await release.wait()
            return await prepare(**kwargs)

        monkeypatch.setattr(
            root._runner.compaction, "prepare_precompile_admission", gated
        )
        try:
            _seed_history(root)
            usage = await root.read_context_usage()
            ratio = usage["input_tokens"] / usage["input_budget_tokens"]
            root._compaction.policy = replace(
                root._compaction.policy,
                automatic_enabled=True,
                auto_trigger_ratio=ratio * 0.95,
                post_compaction_target_ratio=ratio * 0.85,
            )
            command = random_id("command")
            await root.submit_prompt(
                command_id=command, content=PromptContent.text("ORIGINAL_HIGH_REQUEST")
            )
            await asyncio.wait_for(gate.wait(), 15)
            target = root.active_root_turn_id()
            assert (
                target is not None
                and not model.summary.calls
                and not model.root_requests
            )
            low = replace(high, reasoning=ReasoningEffortSelection("low"))
            await root.update_model_call_binding(low)
            queued = await root.submit_prompt(
                command_id=random_id("command"),
                content=PromptContent.text("STEER_CAPTURED_HIGH"),
            )
            await root.steer_queued_prompt(
                command_id=random_id("command"),
                source_queue_item_id=queued.prompt_delivery.queue_item_id,
                target_turn_id=target,
            )
            release.set()
            await _wait_summary(model, root, command, None)
            assert [call.binding for call in model.summary.calls] == [high]
            assert (
                build_chat_completions_payload(
                    call=model.summary.calls[0], context=model.summary.contexts[0]
                )["reasoning_effort"]
                == "high"
            )
            model.summary.release.set()
            await _complete(root, command)
            assert len(model.root_requests) == 1
            ordinary = model.root_requests[0]
            assert ordinary.prepared_call.call.binding == high
            assert _request_payload(ordinary)["reasoning_effort"] == "high"
            rendered = _render(ordinary.compiled_input.messages)
            assert rendered.index("ORIGINAL_HIGH_REQUEST") < rendered.index(
                "STEER_CAPTURED_HIGH"
            )
            assert await root.model_call_binding() == low
        finally:
            release.set()
            model.summary.release.set()
            model.child_release.set()
            await core.shutdown()

    asyncio.run(scenario())


def test_each_queued_request_keeps_its_own_binding_when_latest_selection_changes(
    tmp_path,
    monkeypatch,
    stage2_migrated_postgres_database,
):
    async def scenario():
        core, root, model, high, _ = await _open(
            tmp_path, monkeypatch, stage2_migrated_postgres_database.runtime_dsn
        )
        model.hold_first = True
        running = None
        try:
            running = asyncio.create_task(
                root.run_turn(
                    PromptContent.text("FIRST_HIGH"), command_id=random_id("command")
                )
            )
            await asyncio.wait_for(model.ordinary_started.wait(), 15)
            low = replace(high, reasoning=ReasoningEffortSelection("low"))
            await root.update_model_call_binding(low)
            second = await root.submit_prompt(
                command_id=random_id("command"),
                content=PromptContent.text("SECOND_LOW"),
            )
            await root.update_model_call_binding(high)
            third = await root.submit_prompt(
                command_id=random_id("command"),
                content=PromptContent.text("THIRD_HIGH"),
            )
            medium = replace(high, reasoning=ReasoningEffortSelection("medium"))
            await root.update_model_call_binding(medium)
            model.ordinary_release.set()
            await asyncio.wait_for(running, 20)
            await _complete(root, second.command_id)
            await _complete(root, third.command_id)
            assert [
                request.prepared_call.call.binding for request in model.root_requests
            ] == [high, low, high]
            assert [
                _request_payload(request)["reasoning_effort"]
                for request in model.root_requests
            ] == ["high", "low", "high"]
            assert len({request.turn_id for request in model.root_requests}) == 3
            assert await root.model_call_binding() == medium
        finally:
            model.ordinary_release.set()
            model.summary.release.set()
            model.child_release.set()
            if running is not None:
                await asyncio.gather(running, return_exceptions=True)
            await core.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("other_model", [False, True])
def test_manual_summary_keeps_the_accepted_target_after_settings_edit(
    tmp_path,
    monkeypatch,
    stage2_migrated_postgres_database,
    other_model,
):
    async def scenario():
        core, root, model, high, other = await _open(
            tmp_path,
            monkeypatch,
            stage2_migrated_postgres_database.runtime_dsn,
            second_connection=other_model,
        )
        pending = None
        try:
            _seed_history(root)
            original_bindings = _rows(
                root,
                "SELECT id,model_call_binding FROM pulsara_v3.turns WHERE session_id=%s ORDER BY accepted_at,id",
                (root.session_id,),
            )
            selected = other if other_model else high
            await root.update_model_call_binding(selected)
            command = random_id("command")
            pending = asyncio.create_task(root.compact_context(command_id=command))
            await _wait_summary(model, root, command, pending)
            assert root.active_root_turn_id() is None
            assert root.active_manual_compaction_command_id() == command
            await root.update_model_call_binding(
                replace(high, reasoning=ReasoningEffortSelection("low"))
            )
            assert (
                len(model.summary.calls) == 1
                and model.summary.calls[0].binding == selected
            )
            assert (
                build_chat_completions_payload(
                    call=model.summary.calls[0], context=model.summary.contexts[0]
                )["reasoning_effort"]
                == "high"
            )
            assert model.summary.calls[0].target.fact.model_id == (
                "test-other" if other_model else "test-pro"
            )
            model.summary.release.set()
            outcome = await asyncio.wait_for(pending, 20)
            assert outcome.disposition.value == "COMPACTED"
            assert not model.root_requests
            assert (
                _rows(
                    root,
                    "SELECT id,model_call_binding FROM pulsara_v3.turns WHERE session_id=%s ORDER BY accepted_at,id",
                    (root.session_id,),
                )
                == original_bindings
            )
        finally:
            model.summary.release.set()
            model.child_release.set()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            await core.shutdown()

    asyncio.run(scenario())


def test_manual_settings_edit_before_command_acceptance_rejects_frozen_target(
    tmp_path,
    monkeypatch,
    stage2_migrated_postgres_database,
):
    async def scenario():
        core, root, model, high, _ = await _open(
            tmp_path, monkeypatch, stage2_migrated_postgres_database.runtime_dsn
        )
        entered, release = Event(), Event()
        accept = root.repository.accept_manual_compaction_command
        candidates = []

        def gated_accept(guard, *, candidate, deadline_monotonic):
            candidates.append(candidate)
            entered.set()
            assert release.wait(20), "manual admission test barrier timed out"
            return accept(
                guard, candidate=candidate, deadline_monotonic=deadline_monotonic
            )

        monkeypatch.setattr(
            root.repository, "accept_manual_compaction_command", gated_accept
        )
        pending = None
        try:
            _seed_history(root)
            command = random_id("command")
            pending = asyncio.create_task(root.compact_context(command_id=command))
            assert await asyncio.to_thread(entered.wait, 15)
            assert len(candidates) == 1
            assert candidates[0].model_call_binding == high
            assert root.active_root_turn_id() is None
            assert root.active_manual_compaction_command_id() == command
            assert not model.summary.calls
            low = replace(high, reasoning=ReasoningEffortSelection("low"))
            await root.update_model_call_binding(low)
            release.set()
            outcome = await asyncio.wait_for(pending, 20)
            assert outcome.disposition.value == "FAILED"
            assert outcome.public_code == "manual compaction model selection changed"
            assert len(candidates) == 1
            assert not model.summary.calls and not model.root_requests
            assert not _rows(
                root,
                "SELECT command_id FROM pulsara_v3.session_commands WHERE session_id=%s AND command_id=%s",
                (root.session_id, command),
            )
            assert root.active_manual_compaction_command_id() is None
            assert await root.model_call_binding() == low
            await root.run_turn(
                PromptContent.text("AFTER_REJECTED_MANUAL"),
                command_id=random_id("command"),
            )
            assert len(model.root_requests) == 1
            assert model.root_requests[0].prepared_call.call.binding == low
        finally:
            release.set()
            model.summary.release.set()
            model.child_release.set()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            await core.shutdown()

    asyncio.run(scenario())


def test_waiting_work_keeps_accepted_binding_after_parent_selection_changes(
    tmp_path,
    monkeypatch,
    stage2_migrated_postgres_database,
):
    async def scenario():
        core, root, model, high, _ = await _open(
            tmp_path, monkeypatch, stage2_migrated_postgres_database.runtime_dsn
        )
        model.delegate_work = True
        try:
            await root.run_turn(
                PromptContent.text("DELEGATE_FIRST"),
                command_id=random_id("command"),
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            )
            await asyncio.wait_for(model.child_started.wait(), 15)
            child = model.child_requests[0].session_id
            model.reused_agent = child
            low = replace(high, reasoning=ReasoningEffortSelection("low"))
            await root.update_model_call_binding(low)
            await root.run_turn(
                PromptContent.text("DELEGATE_QUEUED"),
                command_id=random_id("command"),
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            )
            rows = _rows(
                root,
                "SELECT status,model_call_binding FROM pulsara_v3.prompt_queue_items WHERE session_id=%s AND input_origin='AGENT_INSTRUCTION' ORDER BY queue_sequence",
                (child,),
            )
            assert [row["status"] for row in rows] == ["CONSUMED", "PENDING"]
            assert rows[1]["model_call_binding"]["reasoning"] == {
                "kind": "effort",
                "value": "low",
            }
            await root.update_model_call_binding(
                replace(high, reasoning=ReasoningEffortSelection("medium"))
            )
            model.child_release.set()

            async def both_finished():
                works = await root._io.run(
                    root.repository.list_agent_works,
                    root_session_id=root.session_id,
                    agent_id=child,
                    limit=10,
                    deadline_monotonic=root._canonical_deadline(),
                )
                return len(works) == 2 and all(
                    work["status"] == "completed" for work in works
                )

            await _eventually(both_finished)
            assert [
                request.prepared_call.call.binding for request in model.child_requests
            ] == [high, low]
            assert [
                _request_payload(request)["reasoning_effort"]
                for request in model.child_requests
            ] == ["high", "low"]
        finally:
            model.child_release.set()
            model.summary.release.set()
            await core.shutdown()

    asyncio.run(scenario())
