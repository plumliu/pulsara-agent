"""Call-local text shares measured through both real provider wire owners."""

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.assembler import (
    CompletedAssistantMessage,
    CompletedToolCallBlock,
)
from pulsara_agent.conversation_kernel.direct_model import quote_provider_followup_wire_resources
from pulsara_agent.conversation_kernel.runner import (
    ConversationKernelRunner,
    _completed_assistant_semantic_message,
    _ordinary_result_followup_upper,
    _tool_result_closure_message,
)
from pulsara_agent.identity import derive_id
from pulsara_agent.llm.request import ProviderInputUsageAnchor
from pulsara_agent.model_input.lowering import ordinary_tool_result_minimum_upper
from pulsara_agent.primitives.context import canonical_json_bytes, freeze_json
from pulsara_agent.primitives.tool_result_budget import ToolResultPageBudget, with_output_read
from tests.test_stage2_direct_model import _port, _prepared_execution


APIS = ("openai_chat_completions", "openai_responses")


@contextmanager
def _quoted(api, count, *, anchor_delta=0):
    request, _ = _prepared_execution(
        _port(api=api), maximum_input_tokens=8_192, tool_names=("read_file",)
    )
    calls = tuple(CompletedToolCallBlock(
        derive_id("block", "runner-text-budget", str(i)),
        f"provider-call:{i}", "read_file",
        freeze_json({"target": {"kind": "PATH", "path": f"file-{i}.txt"}}),
    ) for i in range(count))
    completed = CompletedAssistantMessage("draft:budget", calls, "")
    anchor = None
    if anchor_delta:
        call = request.prepared_call.call
        plan = request.wire_input_plan
        anchor = ProviderInputUsageAnchor(
            model_call_id=derive_id("call", "runner-text-budget", "anchor"),
            epoch_nonce="epoch", epoch_revision=1,
            connection_id=call.binding.connection_id.value,
            target=call.target.fact,
            route_wire_profile_fingerprint=plan.route_wire_profile_fingerprint,
            reported_model_id=None,
            reported_input_tokens=plan.quote.raw_final_wire_estimated_input_tokens + anchor_delta,
            raw_input_tokens=plan.quote.raw_final_wire_estimated_input_tokens,
            materialization=plan.materialization,
        )
    runner = object.__new__(ConversationKernelRunner)
    runner._continuity = SimpleNamespace(
        current_usage_anchor=lambda _scope: anchor,
        current_view=lambda _scope: SimpleNamespace(
            epoch_nonce="epoch", epoch_revision=1,
            wire_input_plan=request.wire_input_plan, logical_bytes=0,
        ),
    )
    runner._context_source_collector = SimpleNamespace(
        freeze_post_response_call_source_upper=lambda: ()
    )
    runner._hook_output_source_upper = lambda **_kwargs: None
    runner._subagent_runtime = None
    try:
        quote = runner._quote_post_response_resources(
            request=request,
            permit=SimpleNamespace(scope=None, epoch_nonce="epoch", epoch_revision=1),
            collected=SimpleNamespace(completed=completed, provider_replay=None),
            canonical_facts=SimpleNamespace(
                canonical_input=SimpleNamespace(
                    identity=request.compiled_input.canonical_input_identity,
                    canonical_expanded_bytes=0, items=(),
                ), plan_workflow_fact=None,
            ),
        )
        yield runner, request, calls, completed, quote, anchor
    finally:
        request.surface_borrow.close()


def _wire(request, completed, messages, anchor):
    return quote_provider_followup_wire_resources(
        plan=request.wire_input_plan, call=request.prepared_call.call,
        actual_assistant_message=_completed_assistant_semantic_message(completed),
        provider_replay=None, bounded_suffix_messages=tuple(messages), usage_anchor=anchor,
    )


@pytest.mark.parametrize("api", APIS)
def test_eight_small_native_queries_fit_without_eight_full_result_reservations(api):
    with _quoted(api, 8) as (runner, request, calls, completed, quote, anchor):
        budget_tokens = request.wire_input_plan.quote.effective_input_budget_tokens
        runner._require_post_response_resources(quote, effective_input_budget_tokens=budget_tokens)
        old = _wire(request, completed, (
            message for call in calls for message in _ordinary_result_followup_upper(call)[3]
        ), anchor)
        assert old.budget_input_tokens > budget_tokens
        assert quote.followup_wire.budget_input_tokens < budget_tokens
        assert tuple(b.tool_call_id for b in quote.result_page_budgets) == tuple(
            call.tool_call_id for call in calls
        )

        body = '{"items":[],"next":null}'
        messages = []
        for call, page in zip(calls, quote.result_page_budgets, strict=True):
            assert page.fits(body)
            messages.extend((
                _tool_result_closure_message(call.tool_call_id),
                ordinary_tool_result_minimum_upper(call.tool_call_id, body=body),
            ))
        assert _wire(request, completed, messages, anchor).budget_input_tokens <= budget_tokens


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("anchor_delta", (0, 700, -200))
def test_all_text_shares_compose_with_escaping_provenance_and_usage_anchor(api, anchor_delta):
    with _quoted(api, 3, anchor_delta=anchor_delta) as (_, request, calls, completed, quote, anchor):
        maximum = 8_000
        messages = []
        actual_increments = []
        for index, (call, allocated) in enumerate(zip(calls, quote.result_page_budgets, strict=True)):
            target = {
                "kind": "SESSION_ENTRY", "session_id": request.session_id,
                "entry_id": derive_id("entry", "runner-text-budget", str(index)),
            }
            page = replace(allocated, result_target=target)
            provenance = (f"memory:{index}:汉\\\"",)

            def body(size):
                return canonical_json_bytes({"text": "汉🙂\\\"\n" * size, "next": None}).decode()

            low, high, winner = 0, maximum, -1
            while low <= high:
                size = (low + high) // 2
                if page.fits(body(size), model_visible_memory_fact_ids=provenance, include_read=True):
                    winner, low = size, size + 1
                else:
                    high = size - 1
            assert 0 < winner < maximum
            # The assigned provider window, not the standalone 40k boundary,
            # limits this page. A little more remains physically representable.
            assert ToolResultPageBudget(call.tool_call_id, target).fits(
                body(winner + 1), model_visible_memory_fact_ids=provenance, include_read=True
            )
            delivered = with_output_read(body(winner), page.read)
            replacement = ordinary_tool_result_minimum_upper(
                call.tool_call_id, body=delivered,
                model_visible_memory_fact_ids=provenance,
            )
            base_messages = [
                message for candidate in calls
                for message in (
                    _tool_result_closure_message(candidate.tool_call_id),
                    ordinary_tool_result_minimum_upper(
                        candidate.tool_call_id,
                        body='{"status":"CANCELLED_BEFORE_DISPATCH"}',
                    ),
                )
            ]
            base_messages[index * 2 + 1] = replacement
            changed = _wire(request, completed, base_messages, anchor)
            actual_increments.append((
                changed.final_wire_utf8_bytes - quote.followup_wire.final_wire_utf8_bytes,
                changed.budget_input_tokens - quote.followup_wire.budget_input_tokens,
            ))
            messages.extend((_tool_result_closure_message(call.tool_call_id), replacement))

        combined = _wire(request, completed, messages, anchor)
        assert combined.final_wire_utf8_bytes == quote.followup_wire.final_wire_utf8_bytes + sum(
            wire for wire, _tokens in actual_increments
        )
        assert combined.budget_input_tokens == quote.followup_wire.budget_input_tokens + sum(
            tokens for _wire_bytes, tokens in actual_increments
        )
        assert combined.budget_input_tokens <= request.wire_input_plan.quote.effective_input_budget_tokens
        assert combined.budget_input_tokens - combined.raw_final_wire_estimated_input_tokens == anchor_delta
