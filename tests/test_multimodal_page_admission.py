"""Complete multimodal pages share the text carrier and joint request boundary."""
from pulsara_agent.conversation_kernel.prompt_content import MAXIMUM_PROMPT_MULTIPART_BYTES
from datetime import datetime, timezone
from dataclasses import replace
import asyncio
import json
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel import tool_execution as execution_module
from pulsara_agent.conversation_kernel.runner import (
    FrozenPostResponseResourceQuote, _ToolResultResourceQuoteOwner,
    refresh_tool_result_page_resources,
)
from pulsara_agent.conversation_kernel.tool_contracts import (
    FrozenImageToolResourceAllowance,
    FrozenImageToolResourceIncrement,
    KernelToolResult,
)
from pulsara_agent.conversation_kernel.tool_execution import ToolBatchExecutor
from pulsara_agent.identity import derive_id
from pulsara_agent.llm.input import FrozenPromptContent, LLMImagePart, LLMMessage, LLMTextPart, LLMToolCall
from pulsara_agent.llm.request import MAXIMUM_PROVIDER_WIRE_INPUT_BYTES
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.model_input.lowering import ordinary_tool_result_minimum_upper
from pulsara_agent.primitives.context import canonical_json_bytes
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from pulsara_agent.primitives.tool_result_budget import ToolResultPageBudget
from pulsara_agent.tools.builtins.filesystem import parse_view_image_source
from tests.support.images import bound_test_image


CALL = derive_id("call", "multimodal-page-admission")
WORKSPACE = derive_id("workspace", "multimodal-page-admission")
SOURCE = parse_view_image_source({"target": {"kind": "PATH", "path": "recording.mp4"}})


def _content(body=None):
    if body is None:
        body = canonical_json_bytes({"video_frames": True, "frames": [{"n": 1}], "next": None}).decode()
    return FrozenPromptContent((LLMTextPart(body), bound_test_image(
        "image/png", b"validated image", 2, 2, workspace_id=WORKSPACE,
    )))


def _allowance(owner, **limits):
    values = dict(canonical_bytes=100_000, logical_bytes=100_000,
                  wire_bytes=100_000, input_tokens=100_000)
    values.update(limits)
    return FrozenImageToolResourceAllowance(
        call_ordinal=0, tool_call_id=CALL,
        executor_binding_fingerprint="test-binding", quote_owner=owner, **values,
    )


def _unexpected_text_quote(*_args):
    raise AssertionError("multimodal page must use its joint quote, not quote text twice")


def test_raw_text_below_40k_is_rejected_when_complete_outer_carrier_exceeds_it():
    # A valid JSON index is escaped again by the provider-visible result carrier.
    body = canonical_json_bytes({"video_frames": True, "note": '"' * 12_000}).decode()
    assert len(body.encode()) < 40_000
    quotes = []
    allowance = _allowance(SimpleNamespace(quote=lambda **kw: quotes.append(kw)))
    budget = ToolResultPageBudget(CALL, accepts_provider_body=_unexpected_text_quote)
    assert not budget.fits_logical(body)
    assert not allowance.accepts_page(content=_content(body), source=SOURCE,
                                     tool_name="read_video_frames", page_budget=budget)
    assert quotes == []


@pytest.mark.parametrize("tool_name", ["view_image", "read_video_frames"])
def test_complete_page_has_one_joint_quote_with_exact_provenance(tool_name):
    content = _content()
    provenance = (derive_id("memory", "multimodal-page-admission"),)
    quoted = []

    def quote(**kwargs):
        quoted.append(kwargs)
        return FrozenImageToolResourceIncrement(10, 20, 30, 40)

    allowance = _allowance(SimpleNamespace(quote=quote))
    budget = ToolResultPageBudget(CALL, accepts_provider_body=_unexpected_text_quote)
    assert allowance.accepts_page(content=content, source=SOURCE, tool_name=tool_name,
                                  page_budget=budget, model_visible_memory_fact_ids=provenance)
    assert len(quoted) == 1
    assert quoted[0] == dict(tool_call_id=CALL, tool_name=tool_name, source=SOURCE,
                             content=content, model_visible_memory_fact_ids=provenance)


@pytest.mark.parametrize("dimension", ["canonical_bytes", "logical_bytes", "wire_bytes", "input_tokens"])
def test_each_joint_resource_dimension_is_required(dimension):
    allowance = _allowance(SimpleNamespace(quote=lambda **_: FrozenImageToolResourceIncrement(10, 10, 10, 10)),
                           **{dimension: 9})
    assert not allowance.accepts_page(content=_content(), source=SOURCE,
                                     tool_name="read_video_frames", page_budget=ToolResultPageBudget(CALL))


def test_page_budget_must_belong_to_same_call():
    allowance = _allowance(SimpleNamespace())
    with pytest.raises(ValueError, match="own this call"):
        allowance.accepts_page(content=_content(), source=SOURCE, tool_name="read_video_frames",
                               page_budget=ToolResultPageBudget(derive_id("call", "other")))


def test_joint_quote_replaces_text_slot_and_binds_actual_image_carrier(monkeypatch):
    content = _content()
    provenance = (derive_id("memory", "quoted-image"),)
    captured = []

    def quote_suffix(_self, messages):
        captured.append(messages)
        return SimpleNamespace(final_wire_utf8_bytes=111, budget_input_tokens=222)

    monkeypatch.setattr(_ToolResultResourceQuoteOwner, "_quote_suffix", quote_suffix)
    owner = _ToolResultResourceQuoteOwner(
        plan=object(), call=object(), assistant=LLMMessage.assistant_turn(text="read the recording"),
        provider_replay=None, base_suffix_messages=(LLMMessage.tool_result("base", tool_call_id=CALL),),
        base_wire=SimpleNamespace(final_wire_utf8_bytes=100, budget_input_tokens=200),
        result_message_indexes=((CALL, 0),),
    )
    increment = owner.quote(tool_call_id=CALL, source=SOURCE, content=content,
                            tool_name="read_video_frames", model_visible_memory_fact_ids=provenance)
    assert (increment.wire_bytes, increment.input_tokens) == (11, 22)
    assert len(captured) == 1
    result, attachment = captured[0]
    public = json.loads(result.content[0].text)
    assert (result.tool_call_id or public["pulsara_late_tool_outcome"]["tool_call_id"]) == CALL
    # The conservative carrier may be late: use its typed outer result field.
    wrapped = public.get("pulsara_tool_result") or public["pulsara_late_tool_outcome"]["result"]
    assert wrapped["body"] == content.parts[0].text
    assert wrapped["model_visible_memory_ids"] == list(provenance)
    assert tuple(p for p in attachment.content if isinstance(p, LLMImagePart)) == (content.parts[1],)
    source_text = "\n".join(p.text for p in attachment.content if isinstance(p, LLMTextPart))
    assert CALL in source_text
    assert "recording.mp4" in source_text


@pytest.mark.parametrize("api", ["openai_chat_completions", "openai_responses"])
def test_pending_prefix_refresh_requotes_same_batch_and_exact_disjoint_shares(api):
    from pulsara_agent.conversation_kernel.direct_model import quote_provider_followup_wire_resources
    from tests.test_stage2_direct_model import _port, _prepared_execution

    request, _ = _prepared_execution(_port(api=api), maximum_input_tokens=200_000,
                                     tool_names=("read_file", "view_image"))
    try:
        calls = tuple(LLMToolCall(
            id=derive_id("call", "refreshed-page", str(i)),
            name="read_file" if i == 0 else "view_image",
            arguments=canonical_json_bytes({"target": {"kind": "PATH", "path": f"{i}.png"}}).decode(),
        ) for i in range(3))
        assistant = LLMMessage.assistant_turn(text="Inspect these sources", tool_calls=calls)
        suffix = tuple(ordinary_tool_result_minimum_upper(call.id, body="{}") for call in calls)
        wire = quote_provider_followup_wire_resources(
            plan=request.wire_input_plan, call=request.prepared_call.call,
            actual_assistant_message=assistant, provider_replay=None,
            bounded_suffix_messages=suffix,
        )
        old_anchor = object()
        owner = _ToolResultResourceQuoteOwner(
            plan=request.wire_input_plan, call=request.prepared_call.call,
            assistant=assistant, provider_replay=None,
            base_suffix_messages=suffix, base_wire=wire,
            result_message_indexes=tuple((call.id, i) for i, call in enumerate(calls)),
            usage_anchor=old_anchor,
        )
        budgets = tuple(ToolResultPageBudget(call.id, result_target={
            "kind": "SESSION_ENTRY", "session_id": request.session_id,
            "entry_id": derive_id("entry", "refreshed-page", str(i)),
        }) for i, call in enumerate(calls))
        allowances = tuple(FrozenImageToolResourceAllowance(
            call_ordinal=i, tool_call_id=calls[i].id,
            executor_binding_fingerprint=f"binding-{i}", canonical_bytes=0,
            logical_bytes=0, wire_bytes=0, input_tokens=0, quote_owner=owner,
        ) for i in (1, 2))
        original = FrozenPostResponseResourceQuote(
            current_canonical_expanded_bytes=1000, actual_assistant_canonical_bytes=100,
            bounded_followup_canonical_bytes=200, canonical_upper_after=1300,
            current_epoch_logical_bytes=2000, actual_assistant_logical_bytes=80,
            bounded_followup_logical_bytes=160, logical_upper_after=2240,
            current_canonical_items=5, bounded_followup_items=6, item_upper_after=12,
            followup_wire=wire, image_call_allowances=allowances, result_page_budgets=budgets,
        )
        fresh, fresh_budgets = refresh_tool_result_page_resources(
            original_quote=original, prepared_call=request.prepared_call,
            compiled_input=request.compiled_input, wire_input_plan=request.wire_input_plan,
        )
        new_owner = fresh[0].quote_owner
        assert new_owner is not owner and all(item.quote_owner is new_owner for item in fresh)
        assert new_owner.plan is request.wire_input_plan and new_owner.call is request.prepared_call.call
        assert new_owner.usage_anchor is None and owner.usage_anchor is old_anchor
        assert new_owner.base_wire == wire
        assert new_owner.assistant is owner.assistant and new_owner.provider_replay is owner.provider_replay
        assert new_owner.base_suffix_messages is owner.base_suffix_messages
        assert new_owner.result_message_indexes is owner.result_message_indexes
        remaining = (
            MAXIMUM_PROMPT_MULTIPART_BYTES,
            MAXIMUM_PROMPT_MULTIPART_BYTES,
            MAXIMUM_PROVIDER_WIRE_INPUT_BYTES - wire.final_wire_utf8_bytes,
            request.wire_input_plan.quote.effective_input_budget_tokens - wire.budget_input_tokens,
        )
        for index, item in enumerate(fresh):
            assert item.tool_call_id == allowances[index].tool_call_id
            assert item.call_ordinal == allowances[index].call_ordinal
            assert item.executor_binding_fingerprint == allowances[index].executor_binding_fingerprint
            assert item.canonical_bytes == remaining[0]
            assert item.logical_bytes == remaining[1]
            assert item.wire_bytes == remaining[2] // 3 + (item.call_ordinal < remaining[2] % 3)
            assert item.input_tokens == remaining[3] // 3 + (item.call_ordinal < remaining[3] % 3)
            assert item.accepts_page(content=_content(), source=SOURCE, tool_name="view_image",
                                      page_budget=fresh_budgets[item.call_ordinal])
        assert [budget.result_target for budget in fresh_budgets] == [budget.result_target for budget in budgets]
        assert all(budget.fits("{}") for budget in fresh_budgets)
        with pytest.raises(ValueError, match="compiled prefix"):
            refresh_tool_result_page_resources(
                original_quote=original, prepared_call=request.prepared_call,
                compiled_input=replace(request.compiled_input, context_id="different-context"),
                wire_input_plan=request.wire_input_plan,
            )
    finally:
        request.surface_borrow.close()


class _ReachedAcceptancePath(Exception):
    pass


def _settle(executor, content, *, budget=None):
    return executor._settle_known_tool_result(
        session_id=derive_id("session", "page-admission"),
        turn_id=derive_id("turn", "page-admission"), assistant_entry_id=None,
        tool_name="read_video_frames", tool_call_id=CALL,
        invocation_arguments={"target": {"kind": "PATH", "path": "recording.mp4"}},
        call_ordinal=0, scope_kind=ModelInputScopeKind.ROOT,
        result_id=derive_id("result", "page-admission"),
        result_entry_id=derive_id("entry", "page-admission"), attempt_id=None,
        workspace_id=WORKSPACE,
        result=KernelToolResult(state="SUCCESS", content=content, page_budget=budget),
        observed_at=datetime.now(timezone.utc), observation_origin=ToolObservationOrigin.BUILTIN,
        live_sink=None, tool_result_block_id=None, live_attribution=None,
    )


def test_final_guard_turns_oversize_image_candidate_into_typed_result():
    seen = []
    executor = object.__new__(ToolBatchExecutor)

    def materialize(**kwargs):
        seen.append(kwargs)
        raise _ReachedAcceptancePath

    async def run(operation, **kwargs):
        return operation(**kwargs)

    executor._io = SimpleNamespace(run=run)
    executor._canonical_deadline = lambda: 1
    executor._tool_output_processor = SimpleNamespace(materialize_body=materialize)
    content = _content(canonical_json_bytes({"video_frames": True, "note": '"' * 12_000}).decode())
    with pytest.raises(_ReachedAcceptancePath):
        asyncio.run(_settle(executor, content))
    assert json.loads(seen[0]["public_output"]) == {"error": "RESULT_BUDGET_EXCEEDED"}


def test_final_guard_keeps_valid_page_when_old_provider_allowance_is_exhausted(monkeypatch):
    seen = []
    executor = object.__new__(ToolBatchExecutor)
    executor._writer_lease = SimpleNamespace(guard=object())

    def prepare(**kwargs):
        seen.append(kwargs)
        raise _ReachedAcceptancePath

    monkeypatch.setattr(execution_module, "build_prepared_tool_result_acceptance", prepare)
    with pytest.raises(_ReachedAcceptancePath):
        asyncio.run(_settle(executor, _content(), budget=ToolResultPageBudget(
            CALL, accepts_provider_body=_unexpected_text_quote,
        )))
    assert seen[0]["result_state"] == "SUCCESS"
    assert seen[0]["canonical_prompt"] is not None
    assert seen[0]["output_body"] is None


@pytest.mark.parametrize("fresh_fits", [True, False])
def test_view_image_refreshes_zero_old_allowance_without_reloading_reference(tmp_path, fresh_fits):
    from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
    from pulsara_agent.conversation_kernel.tool_policy import DefaultToolDispatchAuthorizationPolicy
    from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
    from tests.support.model_config import scheduled_task_service
    from tests.support.round3 import direct_tool_invocation_context

    async def scenario():
        image = _content().parts[1]
        reads, old_quotes, fresh_quotes, refreshes = [], [], [], []

        class ReferencePort:
            def read_image(self, **kwargs):
                reads.append(kwargs)
                assert kwargs["maximum_encoded_bytes"] > len(image.immutable_bytes)
                return image

        def quote_into(sink):
            def quote(**kwargs):
                sink.append(kwargs)
                return FrozenImageToolResourceIncrement(1, 1, 1, 1)
            return quote

        session = derive_id("session", "image-refresh")
        attempt = derive_id("attempt", "image-refresh")
        turn = derive_id("turn", "image-refresh")
        assistant_entry = derive_id("entry", "image-refresh")
        port = DirectKernelToolPort(
            scheduled_tasks=scheduled_task_service(), workspace_root=tmp_path,
            host_owner_id=derive_id("host", "image-refresh"), session_id=session,
            live_bus=LiveAgentEventBus(), authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
            image_reference_read_port=ReferencePort(),
        )
        borrow, context = direct_tool_invocation_context(
            port, session_id=session, workspace_id=WORKSPACE, tool_name="view_image",
            tool_call_id=CALL, attempt_id=attempt, turn_id=turn,
            assistant_entry_id=assistant_entry,
        )
        binding = borrow.execution_binding("view_image")
        old = replace(_allowance(SimpleNamespace(quote=quote_into(old_quotes))),
                      executor_binding_fingerprint=binding.executor_binding_fingerprint,
                      canonical_bytes=0, logical_bytes=0, wire_bytes=0, input_tokens=0)
        fresh = replace(old, quote_owner=SimpleNamespace(quote=quote_into(fresh_quotes)),
                        canonical_bytes=int(fresh_fits), logical_bytes=int(fresh_fits),
                        wire_bytes=int(fresh_fits), input_tokens=int(fresh_fits))

        async def refresh():
            refreshes.append(True)
            return fresh, context.page_budget

        try:
            result = await port.invoke(
                tool_name="view_image", arguments={"target": {"kind": "IMAGE_REF", "image_ref": image.image_ref}},
                tool_call_id=CALL, attempt_id=attempt, turn_id=turn,
                assistant_entry_id=assistant_entry,
                invocation_context=replace(context, input_modalities=("text", "image"),
                                           image_resource_allowance=old, refresh_image_resources=refresh),
            )
            assert len(reads) == len(refreshes) == len(old_quotes) == len(fresh_quotes) == 1
            assert old_quotes[0]["content"] is fresh_quotes[0]["content"]
            if fresh_fits:
                assert result.state == "SUCCESS"
                assert result.content is fresh_quotes[0]["content"]
                assert result.content.parts[1] == image
            else:
                assert result.state == "APPLICATION_ERROR"
                assert json.loads(result.content) == {"error": "IMAGE_RESOURCE_EXCEEDED"}
        finally:
            borrow.close()
            await port.aclose(timeout_seconds=2)

    asyncio.run(scenario())
