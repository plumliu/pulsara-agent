"""Context assembly facade for the main agent loop."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from cachetools import LRUCache

from pulsara_agent.llm.input import LLMMessage, ToolSpec
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.resolution import ResolvedModelCall
from pulsara_agent.llm.estimator import TokenEstimator
from pulsara_agent.message import Msg
from pulsara_agent.event.events import utc_now
from pulsara_agent.runtime.context_engine import (
    CompiledContext,
    ContextBudgetExceeded,
    ContextBudgetReport,
    ContextCompileInputs,
    ContextCompileRequest,
    ContextLifecycleCoordinator,
    compile_context,
)
from pulsara_agent.runtime.context_engine.compiler import build_recovery_message
from pulsara_agent.runtime.context_engine.tool_results import (
    ToolResultRenderDecisionCache,
    commit_tool_result_render_decision_cache,
    make_tool_result_render_decision_cache,
    raise_if_tool_result_budget_unsatisfied,
    render_segmented_llm_messages,
)
from pulsara_agent.runtime.state import LoopBudget, LoopState

if TYPE_CHECKING:
    from pulsara_agent.capability.exposure import CapabilityExposurePlan


DEFAULT_SYSTEM_PROMPT = (
    "You are Pulsara, an agentic coding runtime. Work carefully inside the current "
    "workspace, use tools when needed, and provide concise final answers."
)


def build_llm_context(
    state: LoopState,
    *,
    tools: tuple[ToolSpec, ...],
    system_prompt: str | None,
    budget: LoopBudget,
    context_id: str | None = None,
    model_call_index: int = 0,
    resolved_call: ResolvedModelCall,
    exposure: "CapabilityExposurePlan | None" = None,
    current_user_anchor: str | None = None,
    runtime_session_id: str | None = None,
    component_prompts: tuple[tuple[str, str], ...] = (),
    lifecycle_coordinator: ContextLifecycleCoordinator | None = None,
) -> LLMContext:
    return build_compiled_context(
        state=state,
        tools=tools,
        system_prompt=system_prompt,
        budget=budget,
        context_id=context_id,
        model_call_index=model_call_index,
        resolved_call=resolved_call,
        exposure=exposure,
        current_user_anchor=current_user_anchor,
        runtime_session_id=runtime_session_id,
        component_prompts=component_prompts,
        lifecycle_coordinator=lifecycle_coordinator,
    ).llm_context


def build_compiled_context(
    state: LoopState,
    *,
    tools: tuple[ToolSpec, ...],
    system_prompt: str | None,
    budget: LoopBudget,
    context_id: str | None = None,
    model_call_index: int = 0,
    resolved_call: ResolvedModelCall,
    exposure: "CapabilityExposurePlan | None" = None,
    current_user_anchor: str | None = None,
    runtime_session_id: str | None = None,
    component_prompts: tuple[tuple[str, str], ...] = (),
    lifecycle_coordinator: ContextLifecycleCoordinator | None = None,
    tool_result_render_decision_cache: ToolResultRenderDecisionCache | None = None,
) -> CompiledContext:
    projection = _projection_component(state.memory_projection)
    prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
    actual_context_id = context_id or f"context:{uuid4().hex}"
    anchor = current_user_anchor or _current_user_anchor_for_state(state)
    decision_cache = tool_result_render_decision_cache
    if decision_cache is None:
        scratchpad_cache = state.scratchpad.get("tool_result_render_decision_cache")
        if not isinstance(scratchpad_cache, LRUCache):
            scratchpad_cache = make_tool_result_render_decision_cache()
            state.scratchpad["tool_result_render_decision_cache"] = scratchpad_cache
        decision_cache = scratchpad_cache
    segmented_messages = render_segmented_llm_messages(
        state.messages,
        budget,
        anchor,
        token_estimator=resolved_call.target.token_estimator,
        decision_cache=decision_cache,
    )
    try:
        raise_if_tool_result_budget_unsatisfied(
            context_id=actual_context_id,
            model_call_index=model_call_index,
            segmented_messages=segmented_messages,
        )
    except ContextBudgetExceeded as exc:
        target = resolved_call.target
        exc.budget_report = ContextBudgetReport(
            target_fingerprint=target.fact.target_fingerprint,
            resolved_model_call_id=resolved_call.fact.resolved_model_call_id,
            measurement_stage="tool_result_render",
            total_context_tokens=target.limits.total_context_tokens,
            max_input_tokens=target.limits.max_input_tokens,
            max_output_tokens=target.limits.max_output_tokens,
            effective_output_tokens=target.context_budget.effective_output_tokens,
            safety_margin_tokens=target.context_budget.safety_margin_tokens,
            input_budget_tokens=target.context_budget.input_budget_tokens,
            sections_estimated_tokens=None,
            tools_estimated_tokens=None,
            envelope_estimated_tokens=None,
            allocation_estimated_tokens=None,
            final_payload_estimated_tokens=None,
            non_transcript_baseline_tokens=None,
            transcript_estimated_tokens=None,
            estimator=target.token_estimator.fact,
        )
        raise
    current_user = _message_by_id(state, anchor) if anchor is not None else None
    current_user_input = _message_text(current_user) if current_user is not None else ""
    compiled_at_utc = utc_now()
    user_observed_at_utc = (
        current_user.created_at
        if current_user is not None and current_user.created_at
        else compiled_at_utc
    )
    request = ContextCompileRequest(
        context_id=actual_context_id,
        runtime_session_id=runtime_session_id or state.session_id,
        run_id=state.run_id,
        turn_id=state.turn_id,
        reply_id=state.reply_id,
        model_call_index=model_call_index,
        compiled_at_utc=compiled_at_utc,
        user_observed_at_utc=user_observed_at_utc,
        resolved_call=resolved_call,
        state=state,
        current_user_message=current_user,
        current_user_input=current_user_input,
        current_user_anchor=anchor,
        tools=tools,
        exposure=exposure,
        budget=budget,
    )
    compiled = compile_context(
        request,
        inputs=ContextCompileInputs(
            system_prompt=prompt,
            prior_messages=segmented_messages.full_messages,
            prior_history_messages=segmented_messages.prior_history_messages,
            current_user_messages=segmented_messages.current_user_messages,
            current_run_tail_messages=segmented_messages.current_run_tail_messages,
            recovery_message=build_recovery_message(request),
            component_prompts=(
                *component_prompts,
                *((("memory:projection", projection),) if projection else ()),
            ),
            tool_result_render_decisions=segmented_messages.tool_result_render_decisions,
            tool_result_budget_report=segmented_messages.tool_result_budget_report,
        ),
        lifecycle_coordinator=lifecycle_coordinator,
    )
    commit_stats = commit_tool_result_render_decision_cache(
        decision_cache,
        segmented_messages.tool_result_render_cache_candidates,
    )
    cache_report = compiled.tool_result_budget_report.get("render_decision_cache")
    if isinstance(cache_report, dict):
        cache_report["commit"] = commit_stats
    return compiled


def msg_to_llm_messages(
    messages: list[Msg],
    budget: LoopBudget,
    *,
    token_estimator: TokenEstimator,
) -> tuple[LLMMessage, ...]:
    return render_segmented_llm_messages(
        messages,
        budget,
        anchor=None,
        token_estimator=token_estimator,
    ).full_messages


def _projection_component(projection: dict[str, Any] | None) -> str | None:
    if not projection:
        return None
    projection_kind = projection.get("projection_kind")
    if projection_kind in {"working_context", "mixed"}:
        heading = (
            "Recalled Memory and Recent Working Context "
            "(source=fenced_memory_context; do not write it back as new memory):\n"
            "Recent Working Context is independent from canonical memory search. "
            "An empty memory_search result does not invalidate recent activity shown here."
        )
    else:
        heading = "Recalled Memory (source=fenced_recalled_memory; do not write it back as new memory):"
    return "\n\n".join([heading, _projection_text(projection)])


def _projection_text(projection: dict[str, Any]) -> str:
    summary = projection.get("summary")
    if isinstance(summary, str) and summary:
        return summary
    items = projection.get("items")
    if isinstance(items, list):
        return "\n".join(f"- {item}" for item in items)
    return str(projection)


def _current_user_anchor_for_state(state: LoopState) -> str | None:
    expected = f"user-message:{state.run_id}"
    if any(message.id == expected for message in state.messages):
        return expected
    return None


def _message_by_id(state: LoopState, message_id: str | None) -> Msg | None:
    if message_id is None:
        return None
    matches = [message for message in state.messages if message.id == message_id]
    if len(matches) != 1:
        return None
    return matches[0]


def _message_text(message: Msg | None) -> str:
    if message is None:
        return ""
    parts: list[str] = []
    for block in message.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)
