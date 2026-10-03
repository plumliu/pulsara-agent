"""Same-model budget edits reuse the complete existing three-tier handover."""

import asyncio
import json
from dataclasses import replace
from uuid import uuid4

import pytest

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("tier", (1, 2, 3))
def test_live_context_allowance_preserves_output_and_uses_existing_compaction(
    tmp_path,
    monkeypatch,
    stage2_migrated_postgres_database,
    tier,
):
    from pulsara_agent.conversation_kernel import host as host_module
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.llm.adapters.openai.chat_completions import (
        OpenAIChatCompletionsTransport,
        build_chat_completions_payload,
    )
    from pulsara_agent.llm.input import PromptContent
    from pulsara_agent.primitives.model_call import ModelCallPurpose
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.model_config import (
        test_model_binding,
        test_model_limits,
        test_model_runtime,
    )

    runtime = test_model_runtime(
        wire_api="openai_chat_completions",
        postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
        limits=test_model_limits(
            total_context_tokens=1_000_000,
            max_input_tokens=1_000_000,
            max_output_tokens=384_000,
            default_output_tokens=384_000,
        ),
    )
    if tier == 2:
        settings = runtime.settings.read()
        runtime.settings.value = replace(
            settings,
            model_connections=(
                replace(settings.model_connections[0], context_window_tokens=512_000),
            ),
        )
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "product-home"))
    monkeypatch.setattr(
        host_module.LocalMcpManagementService,
        "load_configs",
        lambda *_args, **_kwargs: (),
    )
    requests = []
    original_stream = OpenAIChatCompletionsTransport.stream

    async def stream(adapter, *, call, context):
        requests.append(
            (
                call.fact.purpose,
                context.provider_wire_input_plan,
                build_chat_completions_payload(call=call, context=context),
                call.target.context_budget.input_budget_tokens,
            )
        )
        adapter._mock_chunks = [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "Completed context summary or response."},
                    }
                ]
            },
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 100
                    if tier == 1 or len(requests) != 1
                    else 300_000,
                    "completion_tokens": 8,
                },
            },
        ]
        async for item in original_stream(adapter, call=call, context=context):
            yield item

    monkeypatch.setattr(OpenAIChatCompletionsTransport, "stream", stream)

    async def scenario():
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(
            HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path)
        )
        try:
            await session.update_model_call_binding(test_model_binding(runtime))
            from tests.dogfood.run_model_switch_handover_dogfood import (
                _seed_completed_history,
            )

            await _seed_completed_history(
                session, segments=3, repetitions=50 if tier == 1 else 3500
            )
            await session.run_turn(
                PromptContent.text("start"),
                command_id="command:allowance:" + uuid4().hex,
            )
            scope = next(iter(session._input_continuity._slots))
            cohort = session._input_continuity.current_cohort(scope)
            anchor = session._input_continuity.current_usage_anchor(scope)
            settings = runtime.settings.read()
            edited = replace(
                settings.model_connections[0], context_window_tokens=256_000
            )
            runtime.settings.value = replace(settings, model_connections=(edited,))
            preview = await session.read_context_usage()
            assert preview["state"] == "ready"
            assert preview["input_budget_tokens"] == 247_807
            assert preview["budget_source"] == "heuristic"
            assert preview["model_switch_pending"] is True
            assert preview["compaction_expected"] is (tier != 1)
            assert session._input_continuity.current_cohort(scope) is cohort
            assert session._input_continuity.current_usage_anchor(scope) is anchor
            assert len(requests) == 1
            if tier == 3:
                # Exercise the existing typed Tier 2 -> Tier 3 preparation seam.
                def unavailable_source(**_kwargs):
                    raise ValueError("installed source target is unavailable")

                monkeypatch.setattr(
                    session._runner._provider_dispatch,
                    "prepare_installed_source_target",
                    unavailable_source,
                )
            await session.run_turn(
                PromptContent.text("continue"),
                command_id="command:allowance:" + uuid4().hex,
            )
            current = session._input_continuity.current_cohort(scope)
            assert current.view.epoch_nonce != cohort.view.epoch_nonce
            if tier == 1:
                assert len(requests) == 2
            else:
                assert len(requests) == 3
                purpose, summary, _, source_budget = requests[1]
                assert purpose is ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY
                assert source_budget == (503_807 if tier == 2 else 247_807)
                assert summary.quote.effective_input_budget_tokens == source_budget
                if tier == 2:
                    before = requests[0][1].materialization
                    after = summary.materialization
                    assert after.root_policy_value == before.root_policy_value
                    assert after.tool_items == before.tool_items
                    assert (
                        after.ordered_input_items[: len(before.ordered_input_items)]
                        == before.ordered_input_items
                    )
            assert requests[-1][1].quote.effective_input_budget_tokens == 247_807
            assert all(
                payload["max_completion_tokens"] == 384_000
                for _, _, payload, _ in requests
            )
        finally:
            await core.close_session(session.host_session_id)
            await core.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "tier,manual,final_reject",
    (
        (1, False, False),
        (2, False, False),
        (3, False, False),
        (1, True, False),
        (1, False, True),
    ),
)
def test_allowance_edit_during_tool_followup_uses_handover(
    tmp_path, monkeypatch, stage2_migrated_postgres_database, tier, manual, final_reject
):
    from pulsara_agent.conversation_kernel import host as host_module
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.llm.adapters.openai.chat_completions import (
        OpenAIChatCompletionsTransport,
        build_chat_completions_payload,
    )
    from pulsara_agent.llm.input import PromptContent
    from pulsara_agent.llm.model_catalog import (
        ReasoningSelectableControls,
        ReasoningEffortChoices,
    )
    from pulsara_agent.llm.model_connections import ReasoningEffortSelection
    from pulsara_agent.primitives.model_call import ModelCallPurpose
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.model_config import (
        test_model_binding,
        test_model_limits,
        test_model_runtime,
    )
    from tests.test_model_context_allowance import set_allowance

    runtime = test_model_runtime(
        wire_api="openai_chat_completions",
        postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
        reasoning=ReasoningSelectableControls(
            effort=ReasoningEffortChoices(("low", "medium", "high"))
        )
        if manual
        else None,
        limits=test_model_limits(
            total_context_tokens=1_000_000,
            max_input_tokens=1_000_000,
            max_output_tokens=384_000,
            default_output_tokens=384_000,
        ),
    )
    set_allowance(runtime, 512_000)
    binding = test_model_binding(runtime)
    if manual:
        binding = replace(binding, reasoning=ReasoningEffortSelection("high"))
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "product-home"))
    monkeypatch.setattr(
        host_module.LocalMcpManagementService, "load_configs", lambda *_a, **_kw: ()
    )
    (tmp_path / "sample.txt").write_text("read me")
    original_stream = OpenAIChatCompletionsTransport.stream
    requests = []
    session = None
    agent_calls = 0
    manual_request = None

    async def stream(adapter, *, call, context):
        nonlocal agent_calls, manual_request
        summary = call.fact.purpose is ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY
        if not summary:
            agent_calls += 1
        requests.append(
            (
                call.fact.purpose,
                call.target.context_budget.input_budget_tokens,
                build_chat_completions_payload(call=call, context=context),
            )
        )
        emit_tool = not summary and (agent_calls == 1 or (manual and agent_calls == 2))
        delta = {"content": "Completed."}
        if emit_tool:
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"call:read:{agent_calls}",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "sample.txt"}),
                        },
                    }
                ]
            }
        adapter._mock_chunks = [
            {"choices": [{"index": 0, "delta": delta}]},
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "tool_calls" if emit_tool else "stop",
                    }
                ]
            },
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": (
                        100_000
                        if manual or final_reject
                        else 300_000
                        if tier != 1
                        else 100
                    )
                    if agent_calls == 1
                    else 100,
                    "completion_tokens": 8,
                },
            },
        ]
        if not summary and agent_calls == 1:
            set_allowance(runtime, 256_000)
            if manual:
                manual_request = asyncio.create_task(
                    session.compact_context(
                        command_id="command:manual:" + uuid4().hex, force=True
                    )
                )
                await asyncio.sleep(0.02)
        async for item in original_stream(adapter, call=call, context=context):
            yield item

    monkeypatch.setattr(OpenAIChatCompletionsTransport, "stream", stream)

    async def scenario():
        nonlocal session
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(
            HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path)
        )
        try:
            await session.update_model_call_binding(binding)
            from tests.dogfood.run_model_switch_handover_dogfood import (
                _seed_completed_history,
            )

            await _seed_completed_history(
                session,
                segments=3,
                repetitions=1000
                if manual or final_reject
                else 50
                if tier == 1
                else 3500,
            )
            if final_reject:
                from pulsara_agent.conversation_kernel.provider_dispatch import (
                    PreparedWireMeasurementDecision,
                )

                coordinator = session._runner.compaction
                original_measure = coordinator.measure_dispatch_wire
                rejected = False

                async def larger_final_input(dispatch, **kwargs):
                    nonlocal rejected
                    decision = await original_measure(dispatch, **kwargs)
                    if (
                        not rejected
                        and dispatch.prepared_call.call.target.fact.context_window_tokens
                        == 256_000
                    ):
                        rejected = True
                        # Inject a final wire expansion at the actual admission seam.
                        # Preview remains below threshold; the complete input cannot fit.
                        quote = replace(
                            decision.quote,
                            generic_wire_estimated_input_tokens=300_000
                            + decision.quote.replaced_generic_wire_estimated_tokens
                            - decision.quote.replay_wire_estimated_tokens,
                            raw_final_wire_estimated_input_tokens=300_000,
                            budget_input_tokens=300_000,
                            budget_source="heuristic",
                            anchor_model_call_id=None,
                            anchor_reported_input_tokens=None,
                            estimated_suffix_tokens=None,
                        )
                        return PreparedWireMeasurementDecision(
                            candidate=decision.candidate, quote=quote
                        )
                    return decision

                monkeypatch.setattr(
                    coordinator, "measure_dispatch_wire", larger_final_input
                )
            if tier == 3:

                def unavailable_source(**_kwargs):
                    raise ValueError("installed source target is unavailable")

                monkeypatch.setattr(
                    session._runner._provider_dispatch,
                    "prepare_installed_source_target",
                    unavailable_source,
                )
            result = await session.run_turn(
                PromptContent.text("read the file and finish"),
                command_id="command:followup:" + uuid4().hex,
            )
            if manual_request is not None:
                outcome = await manual_request
                assert outcome.disposition.value == "COMPACTED"
            assert result.final_text == "Completed."
            if final_reject:
                assert rejected
            budgets = [
                budget
                for purpose, budget, _ in requests
                if purpose is ModelCallPurpose.AGENT_MODEL_LOOP
            ]
            assert budgets == (
                [503_807, 503_807, 247_807] if manual else [503_807, 247_807]
            ), requests
            summaries = [
                budget
                for purpose, budget, _ in requests
                if purpose is ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY
            ]
            assert summaries == (
                [503_807]
                if manual or final_reject or tier == 2
                else [247_807]
                if tier == 3
                else []
            )
            assert all(
                payload["max_completion_tokens"] == 384_000
                for _, _, payload in requests
            )
            if manual:
                assert [
                    payload.get("reasoning_effort")
                    for purpose, _, payload in requests
                    if purpose is ModelCallPurpose.AGENT_MODEL_LOOP
                ] == ["high", "high", "high"]
                assert all(
                    payload.get("reasoning_effort") != "high"
                    for purpose, _, payload in requests
                    if purpose is ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY
                )
        finally:
            await core.close_session(session.host_session_id)
            await core.shutdown()

    asyncio.run(scenario())


def test_queued_child_preserves_accepted_allowance_then_hands_over_on_followup(
    tmp_path, monkeypatch, stage2_migrated_postgres_database
):
    from time import monotonic
    import psycopg
    from pulsara_agent.conversation_kernel import host as host_module
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.llm.adapters.openai.chat_completions import (
        OpenAIChatCompletionsTransport,
    )
    from pulsara_agent.llm.input import PromptContent, MessageRole, join_text_content
    from pulsara_agent.conversation_kernel.subagents.launch import (
        CanonicalSubagentLaunchPreparationPort,
    )
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.model_config import (
        test_model_binding,
        test_model_limits,
        test_model_runtime,
    )
    from tests.test_model_context_allowance import set_allowance

    runtime = test_model_runtime(
        wire_api="openai_chat_completions",
        postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
        limits=test_model_limits(
            total_context_tokens=1_000_000,
            max_input_tokens=1_000_000,
            max_output_tokens=384_000,
            default_output_tokens=384_000,
        ),
    )
    set_allowance(runtime, 512_000)
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "product-home"))
    monkeypatch.setattr(
        host_module.LocalMcpManagementService, "load_configs", lambda *_a, **_kw: ()
    )
    (tmp_path / "sample.txt").write_text("read me")
    original_stream = OpenAIChatCompletionsTransport.stream
    root_calls = 0
    child_budgets = []
    root_prompt = "delegate a worker"
    accepted = asyncio.Event()
    resume = asyncio.Event()
    original_launch = CanonicalSubagentLaunchPreparationPort.prepare_launch

    async def delayed_launch(port, candidate):
        accepted.set()
        await resume.wait()
        return await original_launch(port, candidate)

    monkeypatch.setattr(
        CanonicalSubagentLaunchPreparationPort, "prepare_launch", delayed_launch
    )

    async def stream(adapter, *, call, context):
        nonlocal root_calls
        is_child = not any(
            root_prompt in join_text_content(message.content)
            for message in context.messages
            if message.role is MessageRole.USER
        )
        if is_child:
            child_budgets.append(call.target.context_budget.input_budget_tokens)
            tool = "read_file" if len(child_budgets) == 1 else None
            arguments = {"path": "sample.txt"}
        else:
            root_calls += 1
            tool = "spawn_agent" if root_calls == 1 else None
            arguments = {"task": "read the sample and finish"}
        delta = (
            {"content": "Completed."}
            if tool is None
            else {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call:" + uuid4().hex,
                        "type": "function",
                        "function": {"name": tool, "arguments": json.dumps(arguments)},
                    }
                ]
            }
        )
        adapter._mock_chunks = [
            {"choices": [{"index": 0, "delta": delta}]},
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop" if tool is None else "tool_calls",
                    }
                ]
            },
            {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 8}},
        ]
        async for item in original_stream(adapter, call=call, context=context):
            yield item

    monkeypatch.setattr(OpenAIChatCompletionsTransport, "stream", stream)

    async def scenario():
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(
            HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path)
        )
        try:
            await session.update_model_call_binding(test_model_binding(runtime))
            root_run = asyncio.create_task(
                session.run_turn(
                    PromptContent.text(root_prompt),
                    command_id="command:queued:" + uuid4().hex,
                )
            )
            await asyncio.wait_for(accepted.wait(), timeout=30)
            with psycopg.connect(
                stage2_migrated_postgres_database.admin_dsn
            ) as connection:
                task_id, fact = connection.execute(
                    "SELECT id,model_target_fact FROM pulsara_v3.subagent_tasks WHERE session_id=%s",
                    (session.session_id,),
                ).fetchone()
            assert fact["context_window_tokens"] == 512_000
            assert not child_budgets
            set_allowance(runtime, 256_000)
            resume.set()
            await asyncio.wait_for(root_run, timeout=30)
            deadline = (
                monotonic() + 30
            )  # Test observation deadline, not a runtime lifetime cap.
            while True:
                task = session.repository.query_subagent_task(
                    session_id=session.session_id,
                    task_id=task_id,
                    deadline_monotonic=deadline,
                )
                if task["status"] in (
                    "COMPLETED",
                    "FAILED",
                    "CANCELLED",
                    "INTERRUPTED",
                ):
                    break
                assert monotonic() < deadline
                await asyncio.sleep(0.02)
            assert task["status"] == "COMPLETED", task
            assert child_budgets == [503_807, 247_807]
        finally:
            await core.close_session(session.host_session_id)
            await core.shutdown()

    asyncio.run(scenario())


def test_headroom_decision_failure_releases_handle_before_next_turn(
    tmp_path,
    monkeypatch,
    stage2_migrated_postgres_database,
):
    from pulsara_agent.conversation_kernel import host as host_module
    from pulsara_agent.conversation_kernel.compaction.coordinator import (
        CompactionCoordinator,
    )
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.conversation_kernel.provider_dispatch import (
        PreparedProviderHeadroomAdmission,
    )
    from pulsara_agent.llm.adapters.openai.chat_completions import (
        OpenAIChatCompletionsTransport,
    )
    from pulsara_agent.llm.input import PromptContent
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.model_config import test_model_binding, test_model_runtime

    runtime = test_model_runtime(
        wire_api="openai_chat_completions",
        postgres_dsn=stage2_migrated_postgres_database.runtime_dsn
    )
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "product-home"))
    monkeypatch.setattr(
        host_module.LocalMcpManagementService, "load_configs", lambda *_a, **_kw: ()
    )
    (tmp_path / "sample.txt").write_text("read me")
    original_stream = OpenAIChatCompletionsTransport.stream
    calls = 0

    async def stream(adapter, *, call, context):
        nonlocal calls
        calls += 1
        delta = (
            {"content": "Completed."}
            if calls > 1
            else {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call:read",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"sample.txt"}',
                        },
                    }
                ],
            }
        )
        adapter._mock_chunks = [
            {"choices": [{"index": 0, "delta": delta}]},
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop" if calls > 1 else "tool_calls",
                    }
                ]
            },
            {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 8}},
        ]
        async for item in original_stream(adapter, call=call, context=context):
            yield item

    original_pending = CompactionCoordinator.context_allowance_change_pending
    original_close = PreparedProviderHeadroomAdmission.close
    closed = []

    def fail_decision(*_args, **_kwargs):
        raise ValueError("injected headroom decision failure")

    def record_close(admission):
        original_close(admission)
        closed.append(admission)

    monkeypatch.setattr(OpenAIChatCompletionsTransport, "stream", stream)
    monkeypatch.setattr(
        CompactionCoordinator, "context_allowance_change_pending", fail_decision
    )
    monkeypatch.setattr(PreparedProviderHeadroomAdmission, "close", record_close)

    async def scenario():
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(
            HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path)
        )
        try:
            await session.update_model_call_binding(test_model_binding(runtime))
            with pytest.raises(ValueError, match="injected headroom"):
                await session.run_turn(
                    PromptContent.text("read the sample"),
                    command_id="command:failed:" + uuid4().hex,
                )
            assert calls == 1
            assert closed
            for admission in closed:
                with pytest.raises(RuntimeError, match="consumed"):
                    admission.take_handle()
            monkeypatch.setattr(
                CompactionCoordinator,
                "context_allowance_change_pending",
                original_pending,
            )
            result = await session.run_turn(
                PromptContent.text("continue"),
                command_id="command:resume:" + uuid4().hex,
            )
            assert result.final_text == "Completed."
        finally:
            await core.close_session(session.host_session_id)
            await core.shutdown()

    asyncio.run(scenario())
