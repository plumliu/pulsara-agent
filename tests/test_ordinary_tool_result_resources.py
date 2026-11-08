"""Minimum feedback admission and actual compiler/wire delivery boundaries."""
from tests.support.images import bound_test_image
from pulsara_agent.identity import derive_id

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.assembler import (
    CompletedAssistantMessage,
    CompletedToolCallBlock,
)
from pulsara_agent.conversation_kernel.direct_model import (
    ProviderFollowupWireResourceQuote,
)
from pulsara_agent.conversation_kernel.runner import (
    ConversationKernelRunner,
    OutputResourceInterruption,
    _ordinary_result_followup_upper,
    _tool_result_closure_message,
)
from pulsara_agent.llm.adapters.openai.chat_completions import chat_semantic_wire_group
from pulsara_agent.llm.adapters.openai.responses import responses_semantic_wire_group
from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV3
from pulsara_agent.model_input.contracts import (
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    ModelInputScopeKind,
    ProviderToolCall,
    ToolResultProviderRenderMode,
)
from pulsara_agent.model_input.lowering import (
    _tool_result_message,
    ordinary_tool_result_minimum_upper,
)
from pulsara_agent.primitives.context import canonical_json_bytes, freeze_json
from pulsara_agent.primitives.tool_observation import (
    MAXIMUM_TOOL_OBSERVATION_DURATION_MICROSECONDS,
    ToolObservationOrigin,
    freeze_tool_observation_timing_fact,
)
from pulsara_agent.primitives.tool_result_projection import (
    classify_tool_result_delivery,
)
from tests.test_catalog_final_wire_resources import (
    _WIRE_APIS,
    _measure,
    _prepare_case,
    _skill_source,
)
from tests.test_round3_structured_model_input_compiler import _tool_result


def _wire(message, api):
    group = (
        chat_semantic_wire_group
        if api == _WIRE_APIS[0]
        else responses_semantic_wire_group
    )
    return group(message)


def _charge(messages, api):
    items = tuple(item for message in messages for item in _wire(message, api))
    estimator = PulsaraHeuristicTokenEstimatorV3()
    return len(canonical_json_bytes(items)), sum(
        estimator.estimate_wire_json_component(item) for item in items
    )


@pytest.mark.parametrize("api", _WIRE_APIS)
@pytest.mark.parametrize(
    "origin",
    tuple(
        o for o in ToolObservationOrigin if o is not ToolObservationOrigin.PLAN_CONTROL
    ),
)
def test_minimum_upper_bounds_known_receipt_normal_and_late_carriers(api, origin):
    call_id = derive_id("call", "minimum-receipt-test")
    body = canonical_json_bytes({"status":"CANCELLED_BEFORE_DISPATCH",
        "read":{"tool":"read_file","arguments":{"target":{"kind":"SESSION_ENTRY",
        "session_id":derive_id("session","minimum-receipt"),
        "entry_id":derive_id("entry","minimum-receipt")}}},
        "message_preview":'escapes\\"汉'}).decode()
    base = _tool_result(body, sequence=3, turn_id='turn:klmmazz67vfgsfsvg6ceqgdsfu')
    durations = (None,) if origin is ToolObservationOrigin.POLICY else (None, MAXIMUM_TOOL_OBSERVATION_DURATION_MICROSECONDS)
    for ids in ((), ("\\" * 4094,), ('"' * 4094,), tuple(f"id:{i}" for i in range(50))):
        upper = _charge((_tool_result_closure_message(call_id),
            ordinary_tool_result_minimum_upper(call_id, body=body,
                model_visible_memory_fact_ids=ids)),api)
        for duration in durations:
            for reported in durations:
                timing = freeze_tool_observation_timing_fact(
                    session_id=derive_id("session","minimum-receipt"),
                    turn_id=derive_id("turn","minimum-receipt"),
                    observed_at=datetime(9999,12,31,23,59,59,999999,tzinfo=timezone.utc),
                    observation_duration_microseconds=duration,
                    tool_reported_duration_microseconds=reported, observation_origin=origin)
                for state in ("SUCCESS","SYSTEM_ERROR","CANCELLED_BEFORE_DISPATCH","PERMISSION_DENIED"):
                    item = replace(base, tool_call_id=call_id, tool_result_context=replace(
                        base.tool_result_context,result_state=state,
                        model_visible_memory_fact_ids=ids,timing=timing))
                    for kind in (FrozenProviderInputItemKind.TOOL_RESULT,FrozenProviderInputItemKind.LATE_TOOL_OUTCOME):
                        actual = _tool_result_message(replace(item,item_kind=kind),body)
                        messages = (actual,) if kind is FrozenProviderInputItemKind.TOOL_RESULT else (_tool_result_closure_message(call_id),actual)
                        charged = _charge(messages,api)
                        assert charged[0] <= upper[0] and charged[1] <= upper[1]


def _results(body, *, provenance=(), plan=False):
    args = freeze_json({"target": {"kind": "PATH", "path": "example.txt"}})
    tool_name = "ask_plan_question" if plan else "read_file"
    assistant = FrozenProviderInputItem(
        FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST,
        'entry:yym6qf3l4xz6437gradtmhjrke',
        2,
        'turn:klmmazz67vfgsfsvg6ceqgdsfu',
        (),
        tool_calls=(ProviderToolCall("call:3", tool_name, args),),
    )
    result = _tool_result(body, sequence=3, turn_id='turn:klmmazz67vfgsfsvg6ceqgdsfu')
    metadata = replace(
        result.tool_result_context,
        model_visible_memory_fact_ids=provenance,
    )
    if plan:
        metadata = replace(
            metadata,
            timing=freeze_tool_observation_timing_fact(
                session_id='session:uol64i4tlakqmd3ow6kdvbdqpy',
                turn_id="turn:" + "a" * 26,
                observed_at=datetime.now(timezone.utc),
                observation_duration_microseconds=None,
                tool_reported_duration_microseconds=None,
                observation_origin=ToolObservationOrigin.PLAN_CONTROL,
            ),
        )
    result = replace(
        result,
        tool_request_entry_id='entry:yym6qf3l4xz6437gradtmhjrke',
        tool_call_ordinal=0,
        tool_call_arguments=args,
        tool_result_context=metadata,
        tool_result_delivery=classify_tool_result_delivery(
            tool_name=tool_name, result_state="SUCCESS"
        ),
    )
    return assistant, result


@pytest.mark.parametrize("api", _WIRE_APIS)
@pytest.mark.parametrize("cold", (False, True))
@pytest.mark.parametrize("provenance", ((), ("\\" * 4094,)))
def test_actual_wire_feedback_never_rewrites_a_frozen_tool_projection(api,cold,provenance):
    items = _results("\\" * 8000,provenance=provenance)
    coordinator,candidate,view,_ = _prepare_case(api,_skill_source(1),cold=cold,
        budget=11000 if provenance else 8000,tool_items=items,tool_names=("read_file",))
    original = candidate.append_result.compiled_input
    assert original.tool_result_decisions[0].selected_mode is ToolResultProviderRenderMode.FULL
    measurement = coordinator._model.freeze_wire_measurement(call=candidate.call,
        compile_binding=candidate.compile_binding,native_projection_set=candidate.native_projection_set,
        semantic_input=candidate.semantic_input,replay_hydration=None,tool_choice=None)
    assert measurement.quote.raw_final_wire_estimated_input_tokens > measurement.quote.effective_input_budget_tokens
    measurement.discard_materialization_to_quote()
    decision = _measure(coordinator,candidate)
    assert decision.candidate.semantic_input.tool_result_decisions[0].selected_mode is ToolResultProviderRenderMode.FULL
    assert decision.wire_input_plan is None
    if view is not None:
        assert candidate.semantic_input.messages[:len(view.messages)] == view.messages


@pytest.mark.parametrize("api", _WIRE_APIS)
@pytest.mark.parametrize("plan", (False, True))
def test_final_wire_feedback_preserves_frozen_and_plan_results(api, plan):
    body = (
        '{"status":"success","plan_control":"QUESTION_ANSWERED","answer_kind":"FREE_TEXT","answer":"'
        + "\\\\" * 8000
        + '"}'
        if plan
        else "\\" * 8000
    )
    items = _results(body, plan=plan)
    coordinator, candidate, _, _ = _prepare_case(
        api,
        _skill_source(1),
        cold=True,
        budget=8000,
        tool_items=items,
        tool_names=("read_file",),
    )
    decision = _measure(coordinator, candidate)
    assert (
        decision.candidate.semantic_input.tool_result_decisions
        == candidate.semantic_input.tool_result_decisions
    )
    assert decision.wire_input_plan is None


def _runner_quote(monkeypatch, calls, api):
    from pulsara_agent.conversation_kernel import runner as module

    wire_plan = SimpleNamespace(
        quote=SimpleNamespace(effective_input_budget_tokens=200000)
    )
    identity = SimpleNamespace(conversation_scope_kind=ModelInputScopeKind.ROOT)
    canonical = SimpleNamespace(identity=identity, canonical_expanded_bytes=0, items=())
    runner = object.__new__(ConversationKernelRunner)
    runner._continuity = SimpleNamespace(
        current_usage_anchor=lambda _scope: None,
        current_view=lambda scope: SimpleNamespace(
            epoch_nonce="epoch",
            epoch_revision=1,
            wire_input_plan=wire_plan,
            logical_bytes=0,
        )
    )
    runner._context_source_collector = SimpleNamespace(
        freeze_post_response_call_source_upper=lambda: ()
    )
    runner._hook_output_source_upper = lambda **kw: None
    runner._subagent_runtime = None
    seen = []

    def quote_wire(**kwargs):
        seen.extend(kwargs["bounded_suffix_messages"])
        size, tokens = _charge(kwargs["bounded_suffix_messages"], api)
        return ProviderFollowupWireResourceQuote(size, tokens, tokens, len(seen))

    monkeypatch.setattr(module, "quote_provider_followup_wire_resources", quote_wire)
    completed = CompletedAssistantMessage("draft:test", tuple(calls), "")
    request = SimpleNamespace(
        wire_input_plan=wire_plan,
        compiled_input=SimpleNamespace(canonical_input_identity=identity),
        prepared_call=SimpleNamespace(call=object()),
    )
    quote = runner._quote_post_response_resources(
        request=request,
        permit=SimpleNamespace(scope=None, epoch_nonce="epoch", epoch_revision=1),
        collected=SimpleNamespace(completed=completed, provider_replay=None),
        canonical_facts=SimpleNamespace(
            canonical_input=canonical, plan_workflow_fact=None
        ),
    )
    return runner, quote, seen


@pytest.mark.parametrize("api", _WIRE_APIS)
def test_pre_effect_wire_minimum_keeps_canonical_and_epoch_maxima(monkeypatch, api):
    calls = [
        CompletedToolCallBlock(
            derive_id("block", "test-fixture", f"block:{i}"), f"call:{i}", "read_file", freeze_json({"target": {"kind": "PATH", "path": "example.txt"}})
        )
        for i in range(3)
    ]
    runner, quote, seen = _runner_quote(monkeypatch, calls, api)
    physical = [_ordinary_result_followup_upper(call) for call in calls]
    assert quote.bounded_followup_canonical_bytes == sum(q[0] for q in physical)
    assert quote.bounded_followup_logical_bytes == sum(q[1] for q in physical)
    old_tokens = _charge(tuple(m for q in physical for m in q[3]), api)[1]
    assert quote.followup_wire.raw_final_wire_estimated_input_tokens < old_tokens
    budget = quote.followup_wire.raw_final_wire_estimated_input_tokens
    runner._require_post_response_resources(quote, effective_input_budget_tokens=budget)
    with pytest.raises(OutputResourceInterruption, match="FOLLOWUP_INPUT_TOKENS"):
        runner._require_post_response_resources(
            quote, effective_input_budget_tokens=budget - 1
        )


@pytest.mark.parametrize("api", _WIRE_APIS)
@pytest.mark.parametrize(
    "name,args",
    (
        ("read_file", {}),
        ("query_capabilities", {}),
        ("query_capabilities", {"target": {"kind": "MCP_TOOL"}}),
        ("ask_plan_question", {}),
    ),
)
def test_pre_effect_text_reserves_small_receipts_and_plan_keeps_atomic_carrier(monkeypatch, api, name, args):
    call = CompletedToolCallBlock('block:b6jcz3ejkiu6yivknhzowsm5eu', "call:1", name, freeze_json(args))
    _, quote, seen = _runner_quote(monkeypatch, [call], api)
    if name == "ask_plan_question":
        assert len(seen) == 1
        assert quote.result_page_budgets == ()
        assert _charge(tuple(seen), api)[1] > 10_000
    else:
        assert len(seen) == 2
        assert tuple(b.tool_call_id for b in quote.result_page_budgets) == (call.tool_call_id,)
        assert _charge(tuple(seen), api)[1] < 1_000


@pytest.mark.postgres
@pytest.mark.parametrize(
    "budget,admitted", ((32000, True), (16000, True), (None, False))
)
def test_real_runner_minimum_gate_precedes_effects_and_admits_small_results(
    stage2_migrated_postgres_database, budget, admitted
):
    import asyncio
    from time import monotonic
    from pulsara_agent.conversation_kernel.repository import (
        ConversationKernelRepository,
    )
    from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
    from pulsara_agent.storage.postgres_connection_provider import (
        PostgresConnectionLane,
    )
    from tests.test_stage2_conversation_runner import (
        _AssertingTool,
        _ScriptedModel,
        _acquire_bound_host_writer,
        _name,
        _named_tool_stream,
        _text_stream,
        StructuredToolPort,
        StaticContextSourceCollector,
        LiveAgentEventBus,
        verified_postgres_provider,
        frozen_test_prompt,
        test_model_runtime,
        test_model_limits,
    )

    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    def new_case(configured_budget):
        session_id = _name("session")
        lease = _acquire_bound_host_writer(
            repository,
            session_id=session_id,
            workspace_id=_name("workspace"),
            writer_owner_id=_name("host"),
            lease_seconds=30,
            deadline_monotonic=monotonic() + 30,
        )
        calls = sum(
            (
                _named_tool_stream(
                    tool_name="exec_process",
                    tool_call_id=f"call:{i}",
                    arguments={"command": "true"},
                )
                for i in range(3)
            ),
            [],
        )
        model = _ScriptedModel([calls, _text_stream("finished")])
        runtime = test_model_runtime(
            model_id="test-pro",
            wire_api=_WIRE_APIS[0],
            limits=test_model_limits(
                total_context_tokens=256000,
                max_input_tokens=configured_budget,
                max_output_tokens=1000,
                default_output_tokens=1000,
                input_safety_margin_tokens=0,
            ),
        )
        model._model_runtime = runtime
        model._preparer = DirectKernelModelPort(model_runtime=runtime)
        tools = _AssertingTool(provider, session_id)
        runner = ConversationKernelRunner(
            model_resolution_snapshot_provider=runtime.freeze_resolution_snapshot,
            repository=repository,
            writer_lease=lease,
            model=model,
            tools=StructuredToolPort(tools),
            live_bus=LiveAgentEventBus(maximum_events=4096),
            context_source_collector=StaticContextSourceCollector(),
        )
        return session_id, model, tools, runner

    session_id, model, tools, runner = new_case(budget or 32000)
    if budget is None:
        measured = {}

        def measure_minimum(quote, *, effective_input_budget_tokens, allow_image_page_recovery):
            assert allow_image_page_recovery is False
            minimum = quote.followup_wire.budget_input_tokens
            measured["minimum"] = minimum
            measured["reserve"] = 32000 - effective_input_budget_tokens
            raise OutputResourceInterruption("MEASURE_MINIMUM_BEFORE_EFFECTS", quote)

        runner._require_post_response_resources = measure_minimum
        with pytest.raises(OutputResourceInterruption, match="MEASURE_MINIMUM"):
            asyncio.run(
                runner.run_turn(frozen_test_prompt("Use three tools then finish."))
            )
        assert tools.invocations == []
        # A fresh, otherwise identical session uses the real owner quote rather
        # than freezing an incidental prompt/schema size into the reject case.
        budget = measured["minimum"] - 1 + measured["reserve"]
        session_id, model, tools, runner = new_case(budget)
        real_require = runner._require_post_response_resources

        def require_below_measured_minimum(quote, *, effective_input_budget_tokens, allow_image_page_recovery):
            assert allow_image_page_recovery is False
            assert effective_input_budget_tokens == measured["minimum"] - 1
            assert quote.followup_wire.budget_input_tokens == measured["minimum"]
            return real_require(
                quote, effective_input_budget_tokens=effective_input_budget_tokens,
                allow_image_page_recovery=allow_image_page_recovery,
            )

        runner._require_post_response_resources = require_below_measured_minimum
    if admitted:
        old_carriers = tuple(
            message
            for i in range(3)
            for message in _ordinary_result_followup_upper(
                CompletedToolCallBlock(
                    derive_id("block", "test-fixture", f"block:{i}"),
                    f"call:{i}",
                    "exec_process",
                    freeze_json({"command": "true"}),
                )
            )[3]
        )
        assert _charge(old_carriers, _WIRE_APIS[0])[1] > budget
        result = asyncio.run(
            runner.run_turn(frozen_test_prompt("Use three tools then finish."))
        )
        assert result.final_text == "finished"
        assert len(tools.invocations) == 3
        assert len(model.requests) == 2
        assert all(
            r.wire_input_plan.quote.raw_final_wire_estimated_input_tokens <= budget
            for r in model.requests
        )
    else:
        with pytest.raises(OutputResourceInterruption) as caught:
            asyncio.run(
                runner.run_turn(frozen_test_prompt("Use three tools then finish."))
            )
        assert caught.value.reason == "FOLLOWUP_INPUT_TOKENS"
        assert tools.invocations == []
        with provider.connection(
            lane=PostgresConnectionLane.INSPECTOR, deadline_monotonic=monotonic() + 10
        ) as connection:
            assert connection.execute(
                "SELECT count(*) FROM pulsara_v3.tool_execution_attempts WHERE session_id=%s",
                (session_id,),
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT count(*) FROM pulsara_v3.transcript_entries WHERE session_id=%s AND entry_kind IN ('ASSISTANT_MESSAGE','ASSISTANT_TOOL_REQUEST','TOOL_RESULT')",
                (session_id,),
            ).fetchone() == (0,)


def test_wire_floor_scan_deadline_releases_measurement_and_runs_off_loop(monkeypatch):
    import asyncio
    import threading
    from time import monotonic
    from pulsara_agent.conversation_kernel.io import KernelSessionIO
    from pulsara_agent.model_input import compiler as module
    from pulsara_agent.model_input.contracts import (
        StructuredModelInputCompileError,
        ModelInputCompileFailureKind,
    )

    coordinator, candidate, _, _ = _prepare_case(
        _WIRE_APIS[0],
        _skill_source(1),
        cold=True,
        budget=8000,
        tool_items=_results("\\" * 8000),
        tool_names=("read_file",),
    )
    lowered = []
    real_lower = module.lower_canonical_item
    real_clock = monotonic
    main_thread = threading.get_ident()
    discarded = []
    freeze = coordinator._model.freeze_wire_measurement

    def capture(**kwargs):
        measurement = freeze(**kwargs)
        discard = measurement.discard_materialization_to_quote

        def counted():
            discarded.append(measurement)
            return discard()

        monkeypatch.setattr(measurement, "discard_materialization_to_quote", counted)
        return measurement

    def lower(*args, **kwargs):
        assert threading.get_ident() != main_thread
        value = real_lower(*args, **kwargs)
        lowered.append(value)
        return value

    monkeypatch.setattr(coordinator._model, "freeze_wire_measurement", capture)
    monkeypatch.setattr(module, "lower_canonical_item", lower)
    monkeypatch.setattr(
        module, "monotonic", lambda: real_clock() + (100 if lowered else 0)
    )

    async def measure():
        io = KernelSessionIO()
        coordinator._io = io
        try:
            return await coordinator.measure_prepared_wire_candidate(
                candidate, deadline=real_clock() + 30
            )
        finally:
            await io.aclose(deadline_monotonic=real_clock() + 30)

    with pytest.raises(StructuredModelInputCompileError) as caught:
        asyncio.run(measure())
    assert caught.value.kind is ModelInputCompileFailureKind.DEADLINE_EXPIRED
    assert len(lowered) == len(discarded) == 1
    with pytest.raises(RuntimeError, match="already consumed"):
        discarded[0].prepare_executable_plan()


@pytest.mark.parametrize("api", _WIRE_APIS)
def test_real_followup_quote_mixes_minimum_results_multiple_images_and_native_replay(
    api,
):
    from pulsara_agent.conversation_kernel.direct_model import (
        quote_provider_followup_wire_resources,
    )
    from pulsara_agent.llm.input import (
        FrozenPromptContent, LLMMessage, frozen_tool_result_public_text,
    )
    from pulsara_agent.llm.provider_replay import (
        build_prepared_durable_provider_assistant_replay,
    )
    from pulsara_agent.model_input.compiler import tool_image_attachment_message
    from pulsara_agent.primitives.context import thaw_json
    from pulsara_agent.tools.builtins.filesystem import parse_view_image_source
    from tests.test_stage2_direct_model import _port, _prepared_execution

    port = _port(api=api)
    request, _ = _prepared_execution(
        port, maximum_input_tokens=200000, tool_names=("read_file", "view_image")
    )
    identity = request.compiled_input.canonical_input_identity
    native_value = (
        {
            "role": "assistant",
            "content": "public",
            "reasoning_content": "opaque\\" * 3000,
        }
        if api == _WIRE_APIS[0]
        else {
            "type": "reasoning",
            "id": "rs:test",
            "summary": [],
            "encrypted_content": "opaque\\" * 3000,
        }
    )
    native = freeze_json(native_value)
    replay = build_prepared_durable_provider_assistant_replay(
        session_id=request.session_id,
        workspace_id='workspace:wdl6b3mtrkghwnvjo4an3ekbau',
        assistant_entry_id='entry:5qidalqvkujk5ykzzzfpwie2qy',
        target=port.replay_target_for_resolved_call(request.prepared_call.call),
        public_projection_fingerprint="sha256:" + "1" * 64,
        ordered_items=(native,),
    )
    calls = (
        CompletedToolCallBlock(
            "b:read", "c:read", "read_file", freeze_json({"target": {"kind": "PATH", "path": "x"}})
        ),
        *(
            CompletedToolCallBlock(
                f"b:{i}", f"c:{i}", "view_image", freeze_json({"target": {"kind": "PATH", "path": f"/tmp/{i}.png"}})
            )
            for i in range(2)
        ),
    )
    runner = object.__new__(ConversationKernelRunner)
    runner._continuity = SimpleNamespace(
        current_usage_anchor=lambda _scope: None,
        current_view=lambda scope: SimpleNamespace(
            epoch_nonce="epoch",
            epoch_revision=1,
            wire_input_plan=request.wire_input_plan,
            logical_bytes=0,
        )
    )
    runner._context_source_collector = SimpleNamespace(
        freeze_post_response_call_source_upper=lambda: ()
    )
    runner._hook_output_source_upper = lambda **kwargs: None
    runner._subagent_runtime = None
    completed = CompletedAssistantMessage("draft:test", calls, "")
    try:
        quote = runner._quote_post_response_resources(
            request=request,
            permit=SimpleNamespace(scope=None, epoch_nonce="epoch", epoch_revision=1),
            collected=SimpleNamespace(completed=completed, provider_replay=replay),
            canonical_facts=SimpleNamespace(
                canonical_input=SimpleNamespace(
                    identity=identity, canonical_expanded_bytes=0, items=()
                ),
                plan_workflow_fact=None,
            ),
        )
        allowances = quote.image_call_allowances
        assert len(allowances) == 2
        owner = allowances[0].quote_owner
        assert allowances[1].quote_owner is owner
        assert owner.provider_replay is replay
        assert owner.base_wire == quote.followup_wire
        carriers, increments = [], []
        text_body = canonical_json_bytes({"lines": ['escapes\\"汉'] * 200, "next": None}).decode()
        text_provenance = ("memory:quoted-source",)
        assert quote.result_page_budgets[0].fits(
            text_body, model_visible_memory_fact_ids=text_provenance
        )
        text_suffix = owner.result_suffix(
            tool_call_id=calls[0].tool_call_id, body=text_body,
            model_visible_memory_fact_ids=text_provenance,
        )
        text_quote = quote_provider_followup_wire_resources(
            plan=request.wire_input_plan, call=request.prepared_call.call, actual_assistant_message=owner.assistant,
            provider_replay=replay, bounded_suffix_messages=text_suffix,
        )
        text_tokens = text_quote.budget_input_tokens - owner.base_wire.budget_input_tokens
        text_wire = text_quote.final_wire_utf8_bytes - owner.base_wire.final_wire_utf8_bytes
        suffix = list(text_suffix)
        positions = dict(owner.result_message_indexes)
        for i, allowance in enumerate(allowances):
            source = parse_view_image_source({"target": {"kind": "PATH", "path": f"/tmp/{i}.png"}})
            image = bound_test_image("image/png", b"x" * (100 + i), 1, 1)
            content = FrozenPromptContent((image,))
            increment = owner.quote(
                tool_call_id=allowance.tool_call_id, source=source, content=content
            )
            assert increment.wire_bytes <= allowance.wire_bytes
            assert increment.input_tokens <= allowance.input_tokens
            increments.append(increment)
            suffix[positions[allowance.tool_call_id]] = owner.result_suffix(
                tool_call_id=allowance.tool_call_id,
                body=frozen_tool_result_public_text(content),
            )[positions[allowance.tool_call_id]]
            carriers.append(
                tool_image_attachment_message(
                    ((allowance.tool_call_id, source, image),)
                )
            )
        combined = quote_provider_followup_wire_resources(
            plan=request.wire_input_plan, call=request.prepared_call.call,
            actual_assistant_message=owner.assistant,
            provider_replay=replay,
            bounded_suffix_messages=(*suffix, *carriers),
        )
        assert (
            combined.raw_final_wire_estimated_input_tokens
            <= request.wire_input_plan.quote.effective_input_budget_tokens
        )
        assert (
            combined.raw_final_wire_estimated_input_tokens
            == owner.base_wire.raw_final_wire_estimated_input_tokens
            + text_tokens
            + sum(i.input_tokens for i in increments)
        )
        assert combined.final_wire_utf8_bytes == (
            owner.base_wire.final_wire_utf8_bytes + text_wire
            + sum(i.wire_bytes for i in increments)
        )
        projection = thaw_json(
            request.wire_input_plan.materialization.context_bearing_projection
        )
        key = "messages" if api == _WIRE_APIS[0] else "input"
        projection[key] += [
            native_value,
            *(
                item
                for message in (*suffix, *carriers)
                for item in _wire(message, api)
            ),
        ]
        assert combined.final_wire_utf8_bytes == len(canonical_json_bytes(projection))
        without_replay = quote_provider_followup_wire_resources(
            plan=request.wire_input_plan, call=request.prepared_call.call,
            actual_assistant_message=LLMMessage.assistant_turn(text="public"),
            provider_replay=None,
            bounded_suffix_messages=owner.base_suffix_messages,
        )
        assert (
            owner.base_wire.final_wire_utf8_bytes
            > without_replay.final_wire_utf8_bytes + 20000
        )
        assert thaw_json(replay.ordered_items[0]) == native_value
        remaining = (request.wire_input_plan.quote.effective_input_budget_tokens
                     - owner.base_wire.budget_input_tokens)
        quotient, remainder = divmod(remaining, len(calls))
        # The first call's text page owns its share; image success includes its
        # receipt replacement and attachment in each of the other two shares.
        assert tuple(a.input_tokens for a in allowances) == tuple(
            quotient + (1 if ordinal < remainder else 0) for ordinal in (1, 2)
        )
        assert sum(a.input_tokens for a in allowances) + quotient + (remainder > 0) == remaining
    finally:
        request.surface_borrow.close()
