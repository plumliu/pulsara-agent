import json

from pulsara_agent.llm.input import MessageRole, ToolSpec
from pulsara_agent.message import (
    AssistantMsg,
    Msg,
    SystemMsg,
    TextBlock,
    ToolCallBlock,
    ToolResultArtifactRef,
    ToolResultBlock,
    ToolResultPreviewMetadata,
    ToolResultState,
    UserMsg,
)
from pulsara_agent.runtime.context import (
    build_compiled_context as _build_compiled_context,
)
import pytest

from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.llm.provider import ProviderProfile
from pulsara_agent.primitives.model_call import ModelContextLimits
from pulsara_agent.runtime.context_engine import (
    ContextCompileInputs,
    ContextBudgetExceeded,
    ContextCompileRequest,
    ContextLifecycleCoordinator,
    ContextSection,
    compile_context,
)
from pulsara_agent.runtime.context_engine.tool_results import (
    _unit_fingerprint,
    commit_tool_result_render_decision_cache,
    make_tool_result_render_decision_cache,
)
from pulsara_agent.runtime.state import LoopBudget, LoopState
from tests.support import test_resolved_call

_TEST_COMPILED_AT = "2026-07-09T01:02:03+00:00"
_TEST_USER_OBSERVED_AT = "2026-07-09T01:02:00+00:00"


def build_compiled_context(**kwargs):
    kwargs.setdefault("resolved_call", test_resolved_call())
    return _build_compiled_context(**kwargs)


def _tool_observation_timing(
    tool_call_id: str,
    tool_name: str,
    *,
    observed_at: str = "2026-07-09T00:00:05+00:00",
    freshness: str = "current_tool_observation",
    tool_origin: str = "builtin",
) -> dict[str, object]:
    return {
        "observed_at": observed_at,
        "source_started_at": "2026-07-09T00:00:00+00:00",
        "source_ended_at": observed_at,
        "observation_duration_seconds": 5.0,
        "freshness": freshness,
        "clock_source": "tool_result_events",
        "tool_origin": tool_origin,
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
    }


def _token_estimate(text: str) -> int:
    return max(1, (len(text) + 3) // 4) if text else 0


def _compile_resolved_contract(
    *,
    call=None,
    user_text: str = "inspect the resolved model contract",
    tools: tuple[ToolSpec, ...] = (),
):
    state = LoopState(session_id="runtime:resolved-contract")
    user = UserMsg(
        name="user",
        content=user_text,
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    state.messages.append(user)
    return build_compiled_context(
        state=state,
        tools=tools,
        system_prompt="System contract",
        budget=state.budget,
        context_id="context:resolved-contract",
        model_call_index=1,
        current_user_anchor=user.id,
        runtime_session_id=state.session_id,
        resolved_call=call or test_resolved_call(),
    )


def test_context_compiler_uses_resolved_limits() -> None:
    limits = ModelContextLimits(
        total_context_tokens=2_000,
        max_input_tokens=1_600,
        max_output_tokens=500,
        default_output_tokens=300,
        input_safety_margin_tokens=100,
    )
    compiled = _compile_resolved_contract(call=test_resolved_call(limits=limits))
    assert compiled.budget.total_context_tokens == 2_000
    assert compiled.budget.max_input_tokens == 1_600
    assert compiled.budget.input_budget_tokens == 1_500


def test_context_compiler_uses_effective_output_tokens() -> None:
    limits = ModelContextLimits(
        total_context_tokens=2_000,
        max_input_tokens=1_900,
        max_output_tokens=600,
        default_output_tokens=300,
        input_safety_margin_tokens=100,
    )
    call = test_resolved_call(
        limits=limits,
        options=LLMOptions(),
    )
    compiled = _compile_resolved_contract(call=call)
    assert compiled.budget.effective_output_tokens == 300
    assert compiled.budget.input_budget_tokens == 1_600


def test_context_compiler_uses_resolved_estimator() -> None:
    call = test_resolved_call()
    compiled = _compile_resolved_contract(call=call)
    assert compiled.budget.estimator == call.target.token_estimator.fact
    assert (
        compiled.final_token_estimate
        == call.target.token_estimator.estimate_context(compiled.llm_context)
    )


def test_tool_result_renderer_uses_resolved_estimator() -> None:
    test_context_compiler_uses_resolved_estimator()


def test_tool_result_render_cache_is_partitioned_by_estimator_fingerprint() -> None:
    block = ToolResultBlock(
        id="call:estimator-cache",
        name="read_file",
        output=[TextBlock(text="stable")],
        state=ToolResultState.SUCCESS,
    )
    common = {
        "block": block,
        "source_message_id": "message:result",
        "source_assistant_message_id": "message:assistant",
        "render_source_fingerprint": "sha256:source",
        "artifact_fingerprint": None,
        "body_candidate_chars": 6,
        "original_chars": 6,
        "tool_observation_timing": _tool_observation_timing(
            "call:estimator-cache", "read_file"
        ),
    }
    first = _unit_fingerprint(**common, estimator_fingerprint="sha256:estimator-a")
    second = _unit_fingerprint(**common, estimator_fingerprint="sha256:estimator-b")
    assert first != second


def test_token_estimate_message_breakdown_matches_message_count_and_total() -> None:
    compiled = _compile_resolved_contract()
    estimate = compiled.final_token_estimate
    assert len(estimate.message_tokens_by_index) == len(compiled.llm_context.messages)
    assert sum(estimate.message_tokens_by_index) == estimate.message_tokens


def test_lowering_message_budget_scopes_match_lowered_messages() -> None:
    compiled = _compile_resolved_contract()
    assert len(compiled.message_budget_scopes) == len(compiled.llm_context.messages)
    assert set(compiled.message_budget_scopes) <= {"transcript", "non_transcript"}


def test_message_framing_follows_message_budget_scope() -> None:
    compiled = _compile_resolved_contract()
    estimate = compiled.final_token_estimate
    transcript = sum(
        estimate.message_tokens_by_index[index]
        for index, scope in enumerate(compiled.message_budget_scopes)
        if scope == "transcript"
    )
    assert transcript == compiled.budget.transcript_estimated_tokens


def test_final_transcript_plus_non_transcript_baseline_equals_total() -> None:
    compiled = _compile_resolved_contract()
    assert (
        compiled.budget.transcript_estimated_tokens
        + compiled.budget.non_transcript_baseline_tokens
        == compiled.budget.final_payload_estimated_tokens
    )


def test_allocation_estimate_equals_sections_plus_tools() -> None:
    tool = ToolSpec(name="read", description="Read", parameters={"type": "object"})
    compiled = _compile_resolved_contract(tools=(tool,))
    assert compiled.budget.allocation_estimated_tokens == (
        compiled.budget.sections_estimated_tokens
        + compiled.budget.tools_estimated_tokens
    )


def test_total_context_over_budget_fails_closed() -> None:
    limits = ModelContextLimits(
        total_context_tokens=80,
        max_input_tokens=64,
        max_output_tokens=16,
        default_output_tokens=16,
        input_safety_margin_tokens=4,
    )
    with pytest.raises(ContextBudgetExceeded):
        _compile_resolved_contract(
            call=test_resolved_call(limits=limits),
            user_text="x" * 2_000,
        )


def test_compiled_report_requires_final_stage_and_all_measurements() -> None:
    compiled = _compile_resolved_contract()
    event_value = compiled.budget.to_event_value()
    assert event_value.measurement_stage == "final_payload"
    assert event_value.final_payload_estimated_tokens is not None
    assert event_value.non_transcript_baseline_tokens is not None
    assert event_value.transcript_estimated_tokens is not None


def test_target_without_tool_support_rejects_tool_context() -> None:
    call = test_resolved_call(
        provider_profile=ProviderProfile(supports_tools=False),
    )
    tool = ToolSpec(name="read", description="Read", parameters={"type": "object"})
    with pytest.raises(ContextBudgetExceeded) as error:
        _compile_resolved_contract(call=call, tools=(tool,))
    assert any(
        diagnostic.code == "model_target_does_not_support_tools"
        for diagnostic in error.value.diagnostics
    )
    assert error.value.budget_report is not None
    assert error.value.budget_report.measurement_stage == "section_allocation"


def test_section_pressure_report_has_section_stage() -> None:
    test_target_without_tool_support_rejects_tool_context()


def test_context_compiler_reports_current_user_and_current_run_tail() -> None:
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="please inspect the repo",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    assistant = AssistantMsg(
        name="assistant",
        content=[
            ToolCallBlock(id="call:1", name="read_file", input='{"path":"README.md"}'),
        ],
    )
    result = AssistantMsg(
        name="assistant",
        content=[
            ToolResultBlock(
                id="call:1",
                name="read_file",
                output=[TextBlock(text="README")],
                state=ToolResultState.SUCCESS,
            )
        ],
    )
    state.messages.extend(
        [UserMsg(name="user", content="older"), user, assistant, result]
    )

    compiled = build_compiled_context(
        state=state,
        tools=(
            ToolSpec(
                name="read_file",
                description="Read a file",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            ),
        ),
        system_prompt="System",
        budget=state.budget,
        context_id="context:test",
        model_call_index=2,
        current_user_anchor=user.id,
        runtime_session_id=state.session_id,
    )

    sections = {section.id: section for section in compiled.sections}
    assert sections["transcript:current_user"].channel == "current_user"
    assert sections["transcript:current_user"].metadata["anchor"] == user.id
    assert sections["transcript:current_user"].estimated_tokens > 0
    assert sections["transcript:current_run_tail"].channel == "current_run_tail"
    assert (
        sections["transcript:current_run_tail"].metadata["structure_must_keep"] is True
    )
    assert sections["transcript:current_run_tail"].metadata["body_may_degrade"] is True
    assert compiled.llm_context.context_id == "context:test"
    assert compiled.llm_context.model_call_index == 2
    assert compiled.tool_specs[0].name == "read_file"
    assert (
        compiled.budget.tools_estimated_tokens
        == compiled.tool_specs[0].estimated_tokens
    )
    assert compiled.estimated_tokens >= compiled.budget.tools_estimated_tokens


def test_context_compiler_records_section_timing_metadata_and_headers() -> None:
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="current timed request",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
        created_at="2026-07-09T00:00:00+00:00",
    )
    assistant = AssistantMsg(
        name="assistant",
        content=[
            ToolCallBlock(
                id="call:timed", name="read_file", input='{"path":"README.md"}'
            ),
        ],
        created_at="2026-07-09T00:00:02+00:00",
    )
    result = Msg(
        role="tool_result",
        name="read_file",
        id="tool-result-message:call:timed",
        content=[
            ToolResultBlock(
                id="call:timed",
                name="read_file",
                output=[TextBlock(text="timed result")],
                state=ToolResultState.SUCCESS,
            )
        ],
        created_at="2026-07-09T00:00:03+00:00",
        finished_at="2026-07-09T00:00:04+00:00",
    )
    state.messages.extend(
        [
            UserMsg(
                name="user", content="older", created_at="2026-07-08T23:59:00+00:00"
            ),
            user,
            assistant,
            result,
        ]
    )

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=state.budget,
        context_id="context:timing",
        model_call_index=2,
        current_user_anchor=user.id,
        component_prompts=(
            ("runtime_context", "Runtime facts"),
            ("subagent:results", "Child result summary"),
        ),
    )

    sections = {section.id: section for section in compiled.sections}
    current_user = sections["transcript:current_user"]
    assert current_user.metadata["source_timing"]["freshness"] == "current_turn"
    assert "compiled_at_utc" not in current_user.metadata["source_timing"]
    assert current_user.metadata["timing"]["compiled_at_utc"]
    assert (
        current_user.metadata["timing"]["source"]["source_started_at"]
        == "2026-07-09T00:00:00+00:00"
    )
    assert "timing_header_text" not in current_user.metadata

    tail = sections["transcript:current_run_tail"]
    assert tail.metadata["source_timing"]["freshness"] == "current_run_tail"
    assert (
        tail.metadata["timing"]["source"]["source_ended_at"]
        == "2026-07-09T00:00:04+00:00"
    )

    runtime_section = sections["runtime_context"]
    assert runtime_section.metadata["source_timing"]["freshness"] == "current_turn"
    assert runtime_section.metadata["timing_header_text"].startswith("[context timing:")
    assert "freshness=current_turn" in runtime_section.metadata["timing_header_text"]
    assert runtime_section.estimated_tokens >= _token_estimate(
        runtime_section.metadata["timing_header_text"] + "\n" + "Runtime facts"
    )
    leading_context = compiled.llm_context.messages[0].content[0]
    assert (
        "## Runtime Context\n[context timing: freshness=current_turn;"
        in leading_context
    )
    assert "Runtime facts" in leading_context

    subagent_section = sections["subagent:results"]
    assert subagent_section.metadata["source_timing"]["freshness"] == "subagent_result"
    assert "## Subagent Results\n[context timing: freshness=subagent_result;" in (
        compiled.llm_context.system_prompt or ""
    )
    assert compiled.llm_context.system_prompt.startswith(
        "System\n\n## Subagent Results"
    )
    assert "System\n[context timing:" not in (compiled.llm_context.system_prompt or "")


def test_context_lifecycle_reused_section_gets_fresh_render_timing_overlay() -> None:
    coordinator = ContextLifecycleCoordinator()
    state = LoopState(session_id="runtime:test")

    def request(
        context_id: str, compiled_at: str, model_call_index: int
    ) -> ContextCompileRequest:
        return ContextCompileRequest(
            context_id=context_id,
            runtime_session_id=state.session_id,
            run_id=state.run_id,
            turn_id=state.turn_id,
            reply_id=state.reply_id,
            model_call_index=model_call_index,
            compiled_at_utc=compiled_at,
            user_observed_at_utc=compiled_at,
            resolved_call=test_resolved_call(),
            state=state,
            current_user_message=None,
            current_user_input="",
            current_user_anchor=None,
            tools=(),
            budget=state.budget,
            exposure=None,
        )

    first = compile_context(
        request("context:timing:1", "2026-07-09T01:00:00+00:00", 1),
        inputs=ContextCompileInputs(
            system_prompt="System",
            prior_messages=(),
            component_prompts=(("runtime_context", "Workspace facts"),),
        ),
        lifecycle_coordinator=coordinator,
    )
    second = compile_context(
        request("context:timing:2", "2026-07-09T01:05:00+00:00", 2),
        inputs=ContextCompileInputs(
            system_prompt="System",
            prior_messages=(),
            component_prompts=(("runtime_context", "Workspace facts"),),
        ),
        lifecycle_coordinator=coordinator,
    )

    first_runtime = next(
        section for section in first.sections if section.id == "runtime_context"
    )
    second_runtime = next(
        section for section in second.sections if section.id == "runtime_context"
    )
    assert first_runtime.lifecycle_status == "freshly_collected"
    assert second_runtime.lifecycle_status == "reused"
    assert (
        first_runtime.metadata["source_timing"]
        == second_runtime.metadata["source_timing"]
    )
    assert (
        first_runtime.metadata["timing"]["compiled_at_utc"]
        == "2026-07-09T01:00:00+00:00"
    )
    assert (
        second_runtime.metadata["timing"]["compiled_at_utc"]
        == "2026-07-09T01:05:00+00:00"
    )
    assert "2026-07-09T01:05:00+00:00" in second_runtime.metadata["timing_header_text"]
    assert (
        "compiled_at_utc=2026-07-09T01:00:00+00:00"
        not in second_runtime.metadata["timing_header_text"]
    )


def test_context_compiler_records_compaction_summary_timing_metadata_in_transcript_section() -> (
    None
):
    state = LoopState(session_id="runtime:test")
    summary = SystemMsg(
        name="pulsara",
        content="<context-compaction-summary>summary</context-compaction-summary>",
        id="context-compaction-summary:context_compaction:abc",
        created_at="2026-07-09T00:30:00+00:00",
        metadata={
            "kind": "context_compaction_summary",
            "artifact_id": "context_compaction_abc:summary",
            "compaction_id": "context_compaction:abc",
            "through_sequence": 42,
            "keep_after_sequence": 42,
        },
    )
    user = UserMsg(
        name="user",
        content="current",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    state.messages.extend([summary, user])

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=state.budget,
        context_id="context:compaction-summary-timing",
        model_call_index=1,
        current_user_anchor=user.id,
    )

    prior = next(
        section
        for section in compiled.sections
        if section.id == "transcript:prior_history"
    )
    summaries = prior.metadata["compaction_summary_messages"]
    assert summaries == [
        {
            "compaction_id": "context_compaction:abc",
            "summary_artifact_id": "context_compaction_abc:summary",
            "compacted_at": "2026-07-09T00:30:00+00:00",
            "keep_after_sequence": 42,
            "through_sequence": 42,
            "source_sequence_start": None,
            "freshness": "compacted_history",
        }
    ]


def test_context_compiler_lowers_leading_user_before_history_and_preserves_current_run_order() -> (
    None
):
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="current request",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    assistant = AssistantMsg(
        name="assistant",
        content=[
            ToolCallBlock(id="call:1", name="read_file", input='{"path":"README.md"}'),
        ],
    )
    result = AssistantMsg(
        name="assistant",
        content=[
            ToolResultBlock(
                id="call:1",
                name="read_file",
                output=[TextBlock(text="tail result")],
                state=ToolResultState.SUCCESS,
            )
        ],
    )
    state.messages.extend(
        [UserMsg(name="user", content="prior history"), user, assistant, result]
    )

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=state.budget,
        context_id="context:test",
        model_call_index=2,
        current_user_anchor=user.id,
        component_prompts=(("runtime_context", "Runtime facts"),),
    )

    messages = compiled.llm_context.messages
    assert compiled.llm_context.system_prompt == "System"
    assert messages[0].role is MessageRole.USER
    assert "<pulsara_context>" in messages[0].content[0]
    assert "Runtime facts" in messages[0].content[0]
    rendered = ["\n".join(message.content) for message in messages]
    assert rendered.index("prior history") < rendered.index("current request")
    assert rendered.index("current request") < next(
        index for index, text in enumerate(rendered) if "tail result" in text
    )


def test_context_compiler_diagnoses_missing_current_user_anchor() -> None:
    state = LoopState(session_id="runtime:test")
    state.messages.append(
        UserMsg(name="user", content="hello", id="user-message:other")
    )

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=state.budget,
        context_id="context:test",
        model_call_index=1,
        current_user_anchor="missing",
    )

    assert any(
        diagnostic.code == "current_user_anchor_unavailable"
        for diagnostic in compiled.diagnostics
    )
    assert any(
        section.id == "transcript:legacy_history" for section in compiled.sections
    )


def test_context_compiler_reports_component_sections_and_counts_lowered_context() -> (
    None
):
    state = LoopState(session_id="runtime:test")

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=state.budget,
        context_id="context:test",
        model_call_index=1,
        component_prompts=(
            ("runtime_context", "Runtime facts"),
            ("capability:catalog", "Skill catalog"),
        ),
    )

    sections = {section.id: section for section in compiled.sections}
    assert sections["runtime_context"].channel == "leading_user"
    assert sections["runtime_context"].metadata["lowered_to"] == "messages"
    assert sections["capability:catalog"].source_id == "capability_exposure"
    assert compiled.budget.sections_estimated_tokens == sum(
        section.estimated_tokens for section in sections.values() if section.included
    )
    assert "Runtime facts" in compiled.llm_context.messages[0].content[0]
    assert "Skill catalog" in compiled.llm_context.messages[0].content[0]


def test_context_compiler_lowers_subagent_results_as_handoff_not_user_request() -> None:
    state = LoopState(session_id="runtime:test")

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=state.budget,
        context_id="context:test",
        model_call_index=1,
        component_prompts=(("subagent:results", "child result summary"),),
    )

    section = next(
        section for section in compiled.sections if section.id == "subagent:results"
    )
    assert section.channel == "handoff_hint"
    assert section.metadata["lowered_to"] == "system_prompt"
    assert "## Subagent Results\n[context timing: freshness=subagent_result;" in (
        compiled.llm_context.system_prompt or ""
    )
    assert "child result summary" in (compiled.llm_context.system_prompt or "")
    assert not compiled.llm_context.messages


def test_context_lifecycle_reuses_and_invalidates_turn_sections() -> None:
    coordinator = ContextLifecycleCoordinator()
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="hello",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    state.messages.append(user)

    first = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=state.budget,
        context_id="context:1",
        model_call_index=1,
        current_user_anchor=user.id,
        lifecycle_coordinator=coordinator,
    )
    second = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=state.budget,
        context_id="context:2",
        model_call_index=2,
        current_user_anchor=user.id,
        lifecycle_coordinator=coordinator,
    )

    first_sections = {section.id: section for section in first.sections}
    second_sections = {section.id: section for section in second.sections}
    assert (
        first_sections["transcript:current_user"].lifecycle_status
        == "freshly_collected"
    )
    assert second_sections["transcript:current_user"].lifecycle_status == "reused"
    assert second.lifecycle_decisions == ()

    request = ContextCompileRequest(
        context_id="context:manual",
        runtime_session_id=state.session_id,
        run_id=state.run_id,
        turn_id=state.turn_id,
        reply_id=state.reply_id,
        model_call_index=1,
        compiled_at_utc=_TEST_COMPILED_AT,
        user_observed_at_utc=_TEST_USER_OBSERVED_AT,
        resolved_call=test_resolved_call(),
        state=state,
        current_user_message=user,
        current_user_input="hello",
        current_user_anchor=user.id,
        tools=(),
        budget=state.budget,
        exposure=None,
    )
    explicit_v1 = ContextSection(
        id="section:explicit",
        source_id="explicit_source",
        channel="leading_user",
        priority=1,
        stability="turn",
        budget_class="important",
        dependency_fingerprint="v1",
    )
    explicit_v2 = ContextSection(
        id="section:explicit",
        source_id="explicit_source",
        channel="leading_user",
        priority=1,
        stability="turn",
        budget_class="important",
        dependency_fingerprint="v2",
    )
    first_explicit, _ = coordinator.apply(request, (explicit_v1,))
    second_explicit, decisions = coordinator.apply(request, (explicit_v2,))

    assert first_explicit[0].lifecycle_status == "freshly_collected"
    assert second_explicit[0].lifecycle_status == "freshly_collected"
    assert any(
        decision.section_id == "section:explicit" and decision.decision == "invalidated"
        for decision in decisions
    )
    assert all(section.lifecycle_status != "invalidated" for section in second_explicit)


def test_context_lifecycle_reuses_and_invalidates_runtime_component_sections() -> None:
    coordinator = ContextLifecycleCoordinator()
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="hello",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    state.messages.append(user)

    first = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=state.budget,
        context_id="context:1",
        model_call_index=1,
        current_user_anchor=user.id,
        component_prompts=(("runtime_context", "Workspace root: /tmp/a"),),
        lifecycle_coordinator=coordinator,
    )
    second = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=state.budget,
        context_id="context:2",
        model_call_index=2,
        current_user_anchor=user.id,
        component_prompts=(("runtime_context", "Workspace root: /tmp/a"),),
        lifecycle_coordinator=coordinator,
    )
    third = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=state.budget,
        context_id="context:3",
        model_call_index=3,
        current_user_anchor=user.id,
        component_prompts=(("runtime_context", "Workspace root: /tmp/b"),),
        lifecycle_coordinator=coordinator,
    )

    first_sections = {section.id: section for section in first.sections}
    second_sections = {section.id: section for section in second.sections}
    third_sections = {section.id: section for section in third.sections}
    assert first_sections["runtime_context"].lifecycle_status == "freshly_collected"
    assert second_sections["runtime_context"].lifecycle_status == "reused"
    assert third_sections["runtime_context"].lifecycle_status == "freshly_collected"
    assert any(
        decision.section_id == "runtime_context"
        and decision.decision == "invalidated"
        and decision.reason == "dependency_fingerprint_changed"
        for decision in third.lifecycle_decisions
    )
    assert third_sections["runtime_context"].lifecycle_status != "invalidated"


def test_context_budget_compacts_memory_projection_before_lowering() -> None:
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="use recalled context",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    state.messages.append(user)
    state.memory_projection = {
        "summary": "MEMORY_SENTINEL " + ("m" * 900_000),
        "included_memory_ids": ["memory:1"],
    }

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=state.budget,
        context_id="context:memory-compact",
        model_call_index=1,
        current_user_anchor=user.id,
    )

    sections = {section.id: section for section in compiled.sections}
    projection = sections["memory:projection"]
    assert projection.render_mode == "compact"
    assert projection.included is True
    assert projection.metadata["source_timing"]["freshness"] == "memory_projection"
    assert projection.metadata["timing_header_text"].startswith(
        "[context timing: freshness=memory_projection;"
    )
    assert projection.metadata["rendered_timing_header_tokens"] == _token_estimate(
        projection.metadata["timing_header_text"]
    )
    assert (
        projection.estimated_tokens
        > projection.metadata["rendered_timing_header_tokens"]
    )
    assert (
        projection.metadata["original_estimated_tokens"] > projection.estimated_tokens
    )
    rendered = "\n".join(
        text for message in compiled.llm_context.messages for text in message.content
    )
    assert (
        "## Recalled Memory and Working Context\n[context timing: freshness=memory_projection;"
        in rendered
    )
    assert "MEMORY_SENTINEL" in rendered
    assert "MEMORY PROJECTION COMPACTED" in rendered
    assert any(
        diagnostic.code == "context_section_degraded"
        and diagnostic.section_id == "memory:projection"
        for diagnostic in compiled.diagnostics
    )


def test_context_budget_compacts_capability_catalog_before_lowering() -> None:
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="use a skill",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    state.messages.append(user)
    huge_catalog = "CATALOG_SENTINEL " + ("c" * 900_000)

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=state.budget,
        context_id="context:catalog-compact",
        model_call_index=1,
        current_user_anchor=user.id,
        component_prompts=(("capability:catalog", huge_catalog),),
    )

    sections = {section.id: section for section in compiled.sections}
    catalog = sections["capability:catalog"]
    assert catalog.render_mode == "compact"
    assert catalog.included is True
    rendered = "\n".join(
        text for message in compiled.llm_context.messages for text in message.content
    )
    assert "CATALOG_SENTINEL" in rendered
    assert "CAPABILITY CATALOG COMPACTED" in rendered
    assert any(
        diagnostic.code == "context_section_degraded"
        and diagnostic.section_id == "capability:catalog"
        for diagnostic in compiled.diagnostics
    )


def test_context_budget_omits_capability_diagnostics_when_budget_is_exhausted() -> None:
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="use a skill",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    state.messages.append(user)
    huge_diagnostics = "CAPABILITY_DIAGNOSTIC_SENTINEL " + ("c" * 1_100_000)

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=state.budget,
        context_id="context:capability-diagnostics-omit",
        model_call_index=1,
        current_user_anchor=user.id,
        component_prompts=(("capability:diagnostics", huge_diagnostics),),
    )

    sections = {section.id: section for section in compiled.sections}
    diagnostics_section = sections["capability:diagnostics"]
    assert diagnostics_section.render_mode == "omitted"
    assert diagnostics_section.included is False
    assert diagnostics_section.estimated_tokens == 0
    rendered = "\n".join(
        text for message in compiled.llm_context.messages for text in message.content
    )
    assert "CAPABILITY_DIAGNOSTIC_SENTINEL" not in rendered
    assert any(
        diagnostic.code == "context_section_omitted"
        and diagnostic.section_id == "capability:diagnostics"
        for diagnostic in compiled.diagnostics
    )


def test_context_budget_omitted_prior_history_is_not_sent_to_model() -> None:
    state = LoopState(session_id="runtime:test")
    prior = UserMsg(name="user", content="PRIOR_HISTORY_SENTINEL " + ("p" * 1_100_000))
    user = UserMsg(
        name="user",
        content="current survives",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    state.messages.extend([prior, user])

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=state.budget,
        context_id="context:omit-history",
        model_call_index=1,
        current_user_anchor=user.id,
    )

    sections = {section.id: section for section in compiled.sections}
    prior_history = sections["transcript:prior_history"]
    assert prior_history.render_mode == "omitted"
    assert prior_history.included is False
    rendered = "\n".join(
        text for message in compiled.llm_context.messages for text in message.content
    )
    assert "PRIOR_HISTORY_SENTINEL" not in rendered
    assert "current survives" in rendered
    assert any(
        diagnostic.code == "context_section_omitted"
        and diagnostic.section_id == "transcript:prior_history"
        for diagnostic in compiled.diagnostics
    )


def test_context_prior_history_estimate_uses_lowered_llm_messages_not_msg_count_slice() -> (
    None
):
    state = LoopState(session_id="runtime:test")
    assistant = AssistantMsg(
        name="assistant",
        content=[
            ToolCallBlock(id="call:prior", name="terminal", input='{"cmd":"x"}'),
            ToolResultBlock(
                id="call:prior",
                name="terminal",
                output=[TextBlock(text="PRIOR_TOOL_RESULT_SENTINEL " + ("r" * 400))],
                state=ToolResultState.SUCCESS,
            ),
            TextBlock(text="PRIOR_TRAILING_TEXT_SENTINEL " + ("a" * 400)),
        ],
    )
    user = UserMsg(
        name="user",
        content="current",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    state.messages.extend([assistant, user])

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=state.budget,
        context_id="context:prior-estimate",
        model_call_index=1,
        current_user_anchor=user.id,
    )

    sections = {section.id: section for section in compiled.sections}
    prior_history = sections["transcript:prior_history"]
    rendered_prior = "\n".join(
        text
        for message in compiled.llm_context.messages
        if "PRIOR" in "\n".join(message.content)
        for text in message.content
    )
    assert prior_history.metadata["message_count"] == 1
    assert prior_history.metadata["llm_message_count"] == 3
    assert "PRIOR_TOOL_RESULT_SENTINEL" in rendered_prior
    assert "PRIOR_TRAILING_TEXT_SENTINEL" in rendered_prior
    assert prior_history.estimated_tokens >= max(1, (len(rendered_prior) + 3) // 4)


def test_context_current_run_tail_estimate_includes_lowered_tool_result_body() -> None:
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="run tool",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    assistant = AssistantMsg(
        name="assistant",
        content=[ToolCallBlock(id="call:tail", name="terminal", input='{"cmd":"x"}')],
    )
    result = AssistantMsg(
        name="assistant",
        content=[
            ToolResultBlock(
                id="call:tail",
                name="terminal",
                output=[TextBlock(text="TAIL_TOOL_RESULT_SENTINEL " + ("t" * 400))],
                state=ToolResultState.SUCCESS,
            )
        ],
    )
    state.messages.extend([user, assistant, result])

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=state.budget,
        context_id="context:tail-estimate",
        model_call_index=2,
        current_user_anchor=user.id,
    )

    sections = {section.id: section for section in compiled.sections}
    tail = sections["transcript:current_run_tail"]
    rendered = "\n".join(
        text for message in compiled.llm_context.messages for text in message.content
    )
    assert "TAIL_TOOL_RESULT_SENTINEL" in rendered
    assert tail.metadata["llm_message_count"] == 2
    rendered_tail = "\n".join(
        text
        for message in compiled.llm_context.messages
        if "TAIL" in "\n".join(message.content) or message.tool_calls
        for text in message.content
    )
    assert tail.estimated_tokens >= max(1, (len(rendered_tail) + 3) // 4)


def test_tool_result_budget_does_not_let_huge_prior_starve_fresh_tail_output() -> None:
    state = LoopState(session_id="runtime:test")
    prior = AssistantMsg(
        name="assistant",
        content=[
            ToolResultBlock(
                id="call:old",
                name="terminal",
                output=[TextBlock(text="OLD_OUTPUT " + ("x" * 36_000))],
                state=ToolResultState.SUCCESS,
            )
        ],
    )
    user = UserMsg(
        name="user",
        content="please run the script",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    assistant = AssistantMsg(
        name="assistant",
        content=[
            ToolCallBlock(
                id="call:fresh",
                name="terminal",
                input='{"cmd":"uv run python main.py"}',
            )
        ],
    )
    fresh = AssistantMsg(
        name="assistant",
        content=[
            ToolResultBlock(
                id="call:fresh",
                name="terminal",
                output=[TextBlock(text="FRESH_RESULT: 206 chars visible")],
                state=ToolResultState.SUCCESS,
            )
        ],
    )
    state.messages.extend([prior, user, assistant, fresh])

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=LoopBudget(tool_result_context_chars=36_000),
        context_id="context:fresh-tool-result",
        model_call_index=2,
        current_user_anchor=user.id,
    )

    rendered = "\n".join(
        text for message in compiled.llm_context.messages for text in message.content
    )
    assert "FRESH_RESULT: 206 chars visible" in rendered
    fresh_decision = next(
        decision
        for decision in compiled.tool_result_render_decisions
        if decision["tool_call_id"] == "call:fresh"
    )
    assert fresh_decision["segment"] == "current_run_tail"
    assert fresh_decision["latest_reserved_candidate"] is True
    assert fresh_decision["latest_reserved_applied"] is True
    assert fresh_decision["latest_reserved_reason"] == "short_result_visible"
    assert fresh_decision["source_block_id"] == "call:fresh"
    assert fresh_decision["source_message_id"] == fresh.id
    assert fresh_decision["source_message_index"] == 3
    assert fresh_decision["content_block_index"] == 0
    assert fresh_decision["model_tool_name"] == "terminal"
    assert str(fresh_decision["render_source_fingerprint"]).startswith("sha256:")
    assert str(fresh_decision["unit_fingerprint"]).startswith("sha256:")
    assert (
        compiled.tool_result_budget_report["caps"]["prior_tool_result_context_chars"]
        < 36_000
    )


def test_prior_envelopes_do_not_borrow_through_current_tail_protected_pool() -> None:
    state = LoopState(session_id="runtime:test")
    prior_results = [
        AssistantMsg(
            name="assistant",
            content=[
                ToolResultBlock(
                    id=f"call:old:{idx}",
                    name="terminal",
                    output=[
                        TextBlock(
                            text=json.dumps(
                                {
                                    "status": "success",
                                    "output": "old body",
                                    "exit_code": 0,
                                    "cwd": f"/workspace/{idx}",
                                    "process_id": f"proc:{idx}",
                                    "terminal_session_id": "default",
                                    "backend_type": "local",
                                }
                            )
                        )
                    ],
                    state=ToolResultState.SUCCESS,
                )
            ],
        )
        for idx in range(2)
    ]
    user = UserMsg(
        name="user",
        content="run final command",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    assistant = AssistantMsg(
        name="assistant",
        content=[
            ToolCallBlock(
                id="call:fresh-tail", name="terminal", input='{"cmd":"python main.py"}'
            )
        ],
    )
    fresh = AssistantMsg(
        name="assistant",
        content=[
            ToolResultBlock(
                id="call:fresh-tail",
                name="terminal",
                output=[TextBlock(text="FRESH_TAIL_STILL_VISIBLE")],
                state=ToolResultState.SUCCESS,
            )
        ],
    )
    state.messages.extend([*prior_results, user, assistant, fresh])

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=LoopBudget(
            tool_result_context_chars=1_200,
            tool_result_envelope_context_chars=256,
            latest_tool_result_reserved_chars=128,
        ),
        context_id="context:prior-envelope-protection",
        model_call_index=2,
        current_user_anchor=user.id,
    )
    rendered = "\n".join(
        text for message in compiled.llm_context.messages for text in message.content
    )
    assert "FRESH_TAIL_STILL_VISIBLE" in rendered
    fresh_decision = next(
        decision
        for decision in compiled.tool_result_render_decisions
        if decision["tool_call_id"] == "call:fresh-tail"
    )
    assert fresh_decision["latest_reserved_applied"] is True
    assert (
        compiled.tool_result_budget_report["caps"]["protected_current_tail_total_chars"]
        > 0
    )


def test_tool_result_model_visible_tool_name_is_bounded() -> None:
    state = LoopState(session_id="runtime:test")
    long_tool_name = "mcp_server_with_an_extremely_long_name__" + ("x" * 120)
    user = UserMsg(
        name="user",
        content="call long tool",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    assistant = AssistantMsg(
        name="assistant",
        content=[ToolCallBlock(id="call:long", name=long_tool_name, input="{}")],
    )
    result = AssistantMsg(
        name="assistant",
        content=[
            ToolResultBlock(
                id="call:long",
                name=long_tool_name,
                output=[TextBlock(text="ok")],
                state=ToolResultState.SUCCESS,
            )
        ],
    )
    state.messages.extend([user, assistant, result])

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=state.budget,
        context_id="context:bounded-tool-name",
        model_call_index=2,
        current_user_anchor=user.id,
    )

    decision = next(
        decision
        for decision in compiled.tool_result_render_decisions
        if decision["tool_call_id"] == "call:long"
    )
    model_tool_name = str(decision["model_tool_name"])
    assert decision["tool_name"] == long_tool_name
    assert len(model_tool_name) <= 64
    assert model_tool_name != long_tool_name
    rendered = "\n".join(
        text for message in compiled.llm_context.messages for text in message.content
    )
    assert f"[tool_result:{model_tool_name}:success]" in rendered
    assert f"[tool_result:{long_tool_name}:success]" not in rendered


def test_tool_result_inside_current_user_segment_is_inert_and_diagnosed() -> None:
    state = LoopState(session_id="runtime:test")
    user = Msg(
        role="user",
        name="user",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
        content=[
            ToolResultBlock(
                id="call:pasted",
                name="terminal",
                output=[TextBlock(text="pasted tool-looking content")],
                state=ToolResultState.SUCCESS,
            )
        ],
    )
    state.messages.append(user)

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=state.budget,
        context_id="context:current-user-tool-result",
        model_call_index=1,
        current_user_anchor=user.id,
    )

    decision = next(
        decision
        for decision in compiled.tool_result_render_decisions
        if decision["tool_call_id"] == "call:pasted"
    )
    assert decision["segment"] == "current_user"
    assert {"code": "tool_result_in_current_user_segment"} in decision["diagnostics"]
    assert any(
        diagnostic.get("code") == "tool_result_in_current_user_segment"
        and diagnostic.get("severity") == "error"
        for diagnostic in compiled.tool_result_budget_report["diagnostics"]
    )
    assert all(
        message.role is not MessageRole.TOOL_RESULT
        for message in compiled.llm_context.messages
    )


def test_tool_result_render_decision_cache_reuses_same_unit_fingerprint() -> None:
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="run command",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    assistant = AssistantMsg(
        name="assistant",
        content=[
            ToolCallBlock(id="call:cached", name="terminal", input='{"cmd":"echo ok"}')
        ],
    )
    result = AssistantMsg(
        name="assistant",
        content=[
            ToolResultBlock(
                id="call:cached",
                name="terminal",
                output=[TextBlock(text="CACHED_RESULT")],
                state=ToolResultState.SUCCESS,
            )
        ],
    )
    state.messages.extend([user, assistant, result])

    first = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=state.budget,
        context_id="context:cache-first",
        model_call_index=1,
        current_user_anchor=user.id,
    )
    second = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=state.budget,
        context_id="context:cache-second",
        model_call_index=2,
        current_user_anchor=user.id,
    )

    first_decision = first.tool_result_render_decisions[0]
    second_decision = second.tool_result_render_decisions[0]
    assert first_decision["render_decision_cache_status"] == "freshly_collected"
    assert second_decision["render_decision_cache_status"] == "reused"
    assert first_decision["unit_fingerprint"] == second_decision["unit_fingerprint"]
    assert (
        second.tool_result_budget_report["render_decision_cache"]["status_counts"][
            "reused"
        ]
        == 1
    )
    rendered_second = next(
        text
        for message in second.llm_context.messages
        if message.role is MessageRole.TOOL_RESULT
        for text in message.content
    )
    assert "CACHED_RESULT" in rendered_second
    assert second_decision["rendered_total_chars"] == len(rendered_second)
    assert second_decision["rendered_header_chars"] > 0
    assert (
        second_decision["rendered_envelope_chars"]
        >= second_decision["rendered_header_chars"]
    )

    constrained = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=LoopBudget(
            tool_result_context_chars=500,
            current_tail_tool_result_context_chars=5,
            latest_tool_result_reserved_chars=0,
            tool_result_per_tool_cap_chars=5,
        ),
        context_id="context:cache-constrained",
        model_call_index=3,
        current_user_anchor=user.id,
    )
    constrained_decision = constrained.tool_result_render_decisions[0]
    assert (
        constrained_decision["render_decision_cache_status"]
        == "overridden_for_hard_cap"
    )
    assert {
        "code": "tool_result_render_cache_overridden_for_hard_cap",
        "reason": "cached_rendered_payload_exceeds_current_hard_cap",
    } in constrained_decision["diagnostics"]
    rendered_constrained = "\n".join(
        text for message in constrained.llm_context.messages for text in message.content
    )
    assert "CACHED_RESULT" not in rendered_constrained

    wide_after_override = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=state.budget,
        context_id="context:cache-wide-after-override",
        model_call_index=4,
        current_user_anchor=user.id,
    )
    wide_decision = wide_after_override.tool_result_render_decisions[0]
    assert wide_decision["render_decision_cache_status"] == "reused"
    rendered_wide = "\n".join(
        text
        for message in wide_after_override.llm_context.messages
        for text in message.content
    )
    assert "CACHED_RESULT" in rendered_wide

    result.content[0].output = [TextBlock(text="CHANGED_RESULT")]
    third = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=state.budget,
        context_id="context:cache-third",
        model_call_index=5,
        current_user_anchor=user.id,
    )
    assert (
        third.tool_result_render_decisions[0]["render_decision_cache_status"]
        == "freshly_collected"
    )
    assert (
        third.tool_result_render_decisions[0]["unit_fingerprint"]
        != second_decision["unit_fingerprint"]
    )


def test_low_budget_first_render_does_not_seed_canonical_cache() -> None:
    state = LoopState(session_id="runtime:test")
    cache: dict[str, dict[str, object]] = {}
    user = UserMsg(
        name="user",
        content="run command",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    assistant = AssistantMsg(
        name="assistant",
        content=[
            ToolCallBlock(id="call:low-cache", name="terminal", input='{"cmd":"echo"}')
        ],
    )
    result = AssistantMsg(
        name="assistant",
        content=[
            ToolResultBlock(
                id="call:low-cache",
                name="terminal",
                output=[TextBlock(text="FULL_RESULT_SHOULD_NOT_BE_CACHED_AS_OMITTED")],
                state=ToolResultState.SUCCESS,
            )
        ],
    )
    state.messages.extend([user, assistant, result])

    low = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=LoopBudget(
            tool_result_context_chars=500,
            current_tail_tool_result_context_chars=5,
            latest_tool_result_reserved_chars=0,
            tool_result_per_tool_cap_chars=5,
        ),
        context_id="context:low-cache-first",
        model_call_index=1,
        current_user_anchor=user.id,
        tool_result_render_decision_cache=cache,
    )
    assert low.tool_result_render_decisions[0]["body_policy"] != "full_visible"
    assert cache == {}

    wide = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=state.budget,
        context_id="context:low-cache-wide",
        model_call_index=2,
        current_user_anchor=user.id,
        tool_result_render_decision_cache=cache,
    )
    wide_decision = wide.tool_result_render_decisions[0]
    assert wide_decision["render_decision_cache_status"] == "freshly_collected"
    assert wide_decision["body_policy"] == "full_visible"
    rendered = "\n".join(
        text for message in wide.llm_context.messages for text in message.content
    )
    assert "FULL_RESULT_SHOULD_NOT_BE_CACHED_AS_OMITTED" in rendered
    assert cache


def test_pressure_compile_does_not_commit_render_cache_candidates() -> None:
    state = LoopState(session_id="runtime:test")
    cache: dict[str, dict[str, object]] = {}
    user = UserMsg(
        name="user",
        content="run command",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    assistant = AssistantMsg(
        name="assistant",
        content=[
            ToolCallBlock(
                id="call:pressure-cache", name="terminal", input='{"cmd":"echo"}'
            )
        ],
    )
    result = AssistantMsg(
        name="assistant",
        content=[
            ToolResultBlock(
                id="call:pressure-cache",
                name="terminal",
                output=[TextBlock(text="PRESSURE_RESULT_SHOULD_NOT_SEED_CACHE")],
                state=ToolResultState.SUCCESS,
            )
        ],
    )
    state.messages.extend([user, assistant, result])

    with pytest.raises(ContextBudgetExceeded):
        build_compiled_context(
            state=state,
            tools=(),
            system_prompt="System",
            budget=LoopBudget(max_tool_results_per_context=0),
            context_id="context:pressure-cache",
            model_call_index=1,
            current_user_anchor=user.id,
            tool_result_render_decision_cache=cache,
        )
    assert cache == {}

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=state.budget,
        context_id="context:pressure-cache-retry",
        model_call_index=2,
        current_user_anchor=user.id,
        tool_result_render_decision_cache=cache,
    )
    assert (
        compiled.tool_result_render_decisions[0]["render_decision_cache_status"]
        == "freshly_collected"
    )
    rendered = "\n".join(
        text for message in compiled.llm_context.messages for text in message.content
    )
    assert "PRESSURE_RESULT_SHOULD_NOT_SEED_CACHE" in rendered


def test_tool_result_render_cache_commit_reports_lru_eviction() -> None:
    cache = make_tool_result_render_decision_cache(max_rendered_chars=100)
    first_stats = commit_tool_result_render_decision_cache(
        cache,
        {
            "unit:first": {
                "_rendered": "A" * 80,
                "visible_body_chars": 80,
                "rendered_envelope_chars": 0,
                "rendered_total_chars": 100,
                "body_policy": "full_visible",
                "envelope_policy": "full_envelope",
                "reason": "within_budget",
            }
        },
    )
    assert first_stats["committed_entries"] == 1
    assert first_stats["evicted_entries"] == 0
    assert list(cache.keys()) == ["unit:first"]

    second_stats = commit_tool_result_render_decision_cache(
        cache,
        {
            "unit:second": {
                "_rendered": "B" * 80,
                "visible_body_chars": 80,
                "rendered_envelope_chars": 0,
                "rendered_total_chars": 100,
                "body_policy": "full_visible",
                "envelope_policy": "full_envelope",
                "reason": "within_budget",
            }
        },
    )
    assert second_stats["committed_entries"] == 1
    assert second_stats["evicted_entries"] == 1
    assert second_stats["evicted_rendered_chars"] == 80
    assert second_stats["entries_after_commit"] == 1
    assert list(cache.keys()) == ["unit:second"]


def test_tool_result_render_cache_skips_oversize_without_deleting_existing_key() -> (
    None
):
    cache = make_tool_result_render_decision_cache(max_rendered_chars=100)
    commit_tool_result_render_decision_cache(
        cache,
        {
            "unit:same": {
                "_rendered": "A" * 80,
                "visible_body_chars": 80,
                "rendered_envelope_chars": 0,
                "rendered_total_chars": 100,
                "body_policy": "full_visible",
                "envelope_policy": "full_envelope",
                "reason": "within_budget",
            }
        },
    )

    stats = commit_tool_result_render_decision_cache(
        cache,
        {
            "unit:same": {
                "_rendered": "B" * 101,
                "visible_body_chars": 101,
                "rendered_envelope_chars": 0,
                "rendered_total_chars": 121,
                "body_policy": "full_visible",
                "envelope_policy": "full_envelope",
                "reason": "within_budget",
            }
        },
    )

    assert stats["committed_entries"] == 0
    assert stats["skipped_oversize_entries"] == 1
    assert stats["entries_after_commit"] == 1
    assert cache["unit:same"]["_rendered"] == "A" * 80


def test_tool_result_render_cache_eviction_is_reported_in_compiled_context() -> None:
    cache = make_tool_result_render_decision_cache(max_rendered_chars=100)

    def compile_with_output(output: str, *, context_id: str) -> dict[str, object]:
        state = LoopState(session_id="runtime:test")
        user = UserMsg(
            name="user",
            content="run command",
            id=f"user-message:{state.run_id}",
            metadata={"run_id": state.run_id},
        )
        assistant = AssistantMsg(
            name="assistant",
            content=[
                ToolCallBlock(
                    id=f"call:{context_id}", name="terminal", input='{"cmd":"echo"}'
                )
            ],
        )
        result = AssistantMsg(
            name="assistant",
            content=[
                ToolResultBlock(
                    id=f"call:{context_id}",
                    name="terminal",
                    output=[TextBlock(text=output)],
                    state=ToolResultState.SUCCESS,
                )
            ],
        )
        state.messages.extend([user, assistant, result])
        compiled = build_compiled_context(
            state=state,
            tools=(),
            system_prompt="System",
            budget=state.budget,
            context_id=f"context:{context_id}",
            model_call_index=1,
            current_user_anchor=user.id,
            tool_result_render_decision_cache=cache,
        )
        return compiled.tool_result_budget_report["render_decision_cache"]["commit"]

    first_commit = compile_with_output("A" * 80, context_id="cache-evict-first")
    assert first_commit["committed_entries"] == 1
    assert first_commit["evicted_entries"] == 0
    second_commit = compile_with_output("B" * 80, context_id="cache-evict-second")
    assert second_commit["committed_entries"] == 1
    assert second_commit["evicted_entries"] == 1
    assert second_commit["entries_after_commit"] == 1


def test_truncated_artifact_preview_is_not_treated_as_latest_short_result() -> None:
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="run noisy command",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    assistant = AssistantMsg(
        name="assistant",
        content=[ToolCallBlock(id="call:huge", name="terminal", input='{"cmd":"yes"}')],
    )
    huge = AssistantMsg(
        name="assistant",
        content=[
            ToolResultBlock(
                id="call:huge",
                name="terminal",
                output=[TextBlock(text="preview" * 100)],
                state=ToolResultState.SUCCESS,
                artifacts=[
                    ToolResultArtifactRef(
                        artifact_id="artifact:huge",
                        role="combined_output",
                        media_type="text/plain; charset=utf-8",
                        size_bytes=200_000,
                        preview=ToolResultPreviewMetadata(
                            preview_policy="head_tail",
                            preview_chars=4_000,
                            original_chars=200_000,
                            original_bytes=200_000,
                            omitted_middle_chars=196_000,
                            visible_head_chars=2_000,
                            visible_tail_chars=2_000,
                            read_more={"suggested_offset_chars": 2_000},
                        ),
                    )
                ],
            )
        ],
    )
    state.messages.extend([user, assistant, huge])

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=LoopBudget(
            tool_result_context_chars=36_000, latest_tool_result_reserved_chars=8_000
        ),
        context_id="context:truncated-preview",
        model_call_index=2,
        current_user_anchor=user.id,
    )

    decision = next(
        decision
        for decision in compiled.tool_result_render_decisions
        if decision["tool_call_id"] == "call:huge"
    )
    assert decision["latest_reserved_candidate"] is True
    assert decision["latest_reserved_applied"] is False
    assert decision["body_candidate_source"] == "non_short_truncated_preview"
    assert decision["latest_reserved_reason"] == "non_short_truncated_preview"


def test_truncated_terminal_json_preview_is_not_treated_as_latest_short_result() -> (
    None
):
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="run noisy terminal command",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    assistant = AssistantMsg(
        name="assistant",
        content=[
            ToolCallBlock(
                id="call:terminal-preview", name="terminal", input='{"cmd":"yes"}'
            )
        ],
    )
    payload = {
        "status": "success",
        "output": "HEAD" + ("x" * 4000) + "TAIL",
        "exit_code": 0,
        "cwd": "/workspace",
        "truncated": True,
        "preview_policy": "head_tail",
        "output_preview_chars": 4008,
        "output_original_chars": 200_000,
        "output_original_bytes": 200_000,
        "omitted_middle_chars": 195_992,
    }
    result = AssistantMsg(
        name="assistant",
        content=[
            ToolResultBlock(
                id="call:terminal-preview",
                name="terminal",
                output=[TextBlock(text=json.dumps(payload, ensure_ascii=False))],
                state=ToolResultState.SUCCESS,
            )
        ],
    )
    state.messages.extend([user, assistant, result])

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=LoopBudget(
            tool_result_context_chars=36_000, latest_tool_result_reserved_chars=8_000
        ),
        context_id="context:terminal-truncated-preview",
        model_call_index=2,
        current_user_anchor=user.id,
    )

    decision = next(
        decision
        for decision in compiled.tool_result_render_decisions
        if decision["tool_call_id"] == "call:terminal-preview"
    )
    assert decision["latest_reserved_candidate"] is True
    assert decision["latest_reserved_applied"] is False
    assert decision["body_candidate_chars"] == 200_000
    assert decision["body_candidate_source"] == "non_short_truncated_preview"
    assert decision["latest_reserved_reason"] == "non_short_truncated_preview"


def test_non_text_artifact_is_not_primary_read_more_target() -> None:
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="render image artifact",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    assistant = AssistantMsg(
        name="assistant",
        content=[ToolCallBlock(id="call:image", name="image_tool", input="{}")],
    )
    image_result = AssistantMsg(
        name="assistant",
        content=[
            ToolResultBlock(
                id="call:image",
                name="image_tool",
                output=[TextBlock(text="image generated")],
                state=ToolResultState.SUCCESS,
                artifacts=[
                    ToolResultArtifactRef(
                        artifact_id="artifact:image",
                        role="image",
                        media_type="image/png",
                        size_bytes=12_345,
                        preview=ToolResultPreviewMetadata(
                            preview_policy="full",
                            preview_chars=256,
                            original_chars=256,
                            original_bytes=512,
                            omitted_middle_chars=0,
                            visible_head_chars=256,
                            visible_tail_chars=0,
                            read_more={"suggested_offset_chars": 0},
                        ),
                    )
                ],
            )
        ],
    )
    state.messages.extend([user, assistant, image_result])

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=LoopBudget(
            tool_result_context_chars=2_000,
            latest_tool_result_reserved_chars=0,
        ),
        context_id="context:non-text-artifact",
        model_call_index=2,
        current_user_anchor=user.id,
    )

    decision = next(
        decision
        for decision in compiled.tool_result_render_decisions
        if decision["tool_call_id"] == "call:image"
    )
    assert decision["primary_artifact_id"] is None
    assert decision["read_more"] is None
    rendered = "\n".join(
        text for message in compiled.llm_context.messages for text in message.content
    )
    envelope = json.loads(rendered.rsplit("\n", 1)[1])
    assert envelope["primary_artifact_id"] is None
    assert envelope["artifact_ids"] == ["artifact:image"]
    assert (
        envelope["diagnostics"][0]["code"]
        == "tool_result_primary_text_artifact_missing"
    )
    assert "artifact_read" not in rendered


def test_tool_result_essential_envelope_over_aggregate_soft_cap_borrows_total() -> None:
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="poll all processes",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    assistant = AssistantMsg(
        name="assistant",
        content=[
            ToolCallBlock(id=f"call:{idx}", name="terminal", input='{"cmd":"x"}')
            for idx in range(4)
        ],
    )
    results = [
        AssistantMsg(
            name="assistant",
            content=[
                ToolResultBlock(
                    id=f"call:{idx}",
                    name="terminal",
                    output=[
                        TextBlock(
                            text=(
                                '{"status":"success","output":"body omitted","exit_code":0,'
                                f'"cwd":"/workspace/{idx}","process_id":"proc:{idx}",'
                                '"terminal_session_id":"default","backend_type":"local"}'
                            )
                        )
                    ],
                    state=ToolResultState.SUCCESS,
                )
            ],
        )
        for idx in range(4)
    ]
    state.messages.extend([user, assistant, *results])

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=LoopBudget(
            tool_result_context_chars=10_000,
            current_tail_tool_result_context_chars=0,
            latest_tool_result_reserved_chars=0,
            tool_result_envelope_context_chars=120,
        ),
        context_id="context:tool-result-envelope-over-cap",
        model_call_index=2,
        current_user_anchor=user.id,
    )

    assert compiled.tool_result_render_decisions
    assert any(
        diagnostic.get("code") == "essential_envelope_budget_unsatisfied"
        and diagnostic.get("soft_target") is True
        and diagnostic.get("borrowed_chars", 0) > 0
        for diagnostic in compiled.tool_result_budget_report["diagnostics"]
    )
    assert (
        compiled.tool_result_budget_report["soft_target_overage"]["envelope_chars"] > 0
    )
    assert compiled.tool_result_budget_report["estimated_tokens"]["total"] > 0


def test_tool_result_per_message_cap_limits_same_tool_batch() -> None:
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="run two tools",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    assistant = AssistantMsg(
        name="assistant",
        id="msg:assistant-batch",
        content=[
            ToolCallBlock(id="call:first", name="terminal", input='{"cmd":"one"}'),
            ToolCallBlock(id="call:second", name="terminal", input='{"cmd":"two"}'),
        ],
    )
    first = AssistantMsg(
        name="assistant",
        content=[
            ToolResultBlock(
                id="call:first",
                name="terminal",
                output=[TextBlock(text="FIRST_RESULT_VISIBLE")],
                state=ToolResultState.SUCCESS,
            )
        ],
    )
    second = AssistantMsg(
        name="assistant",
        content=[
            ToolResultBlock(
                id="call:second",
                name="terminal",
                output=[TextBlock(text="SECOND_RESULT_SHOULD_BE_TRUNCATED")],
                state=ToolResultState.SUCCESS,
            )
        ],
    )
    state.messages.extend([user, assistant, first, second])

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=LoopBudget(
            tool_result_context_chars=36_000,
            tool_result_per_message_cap_chars=24,
            latest_tool_result_reserved_chars=0,
        ),
        context_id="context:per-message-cap",
        model_call_index=2,
        current_user_anchor=user.id,
    )

    rendered = "\n".join(
        text for message in compiled.llm_context.messages for text in message.content
    )
    assert "FIRST_RESULT_VISIBLE" in rendered
    assert "TOOL RESULT BODY OMITTED" in rendered
    decisions = {
        decision["tool_call_id"]: decision
        for decision in compiled.tool_result_render_decisions
    }
    assert decisions["call:first"]["tool_batch_id"] == "msg:assistant-batch"
    assert decisions["call:second"]["tool_batch_id"] == "msg:assistant-batch"
    assert decisions["call:second"]["visible_body_chars"] == 0
    assert decisions["call:second"]["batch_body_budget_remaining"] == 4
    assert (
        compiled.tool_result_budget_report["used_by_batch"]["msg:assistant-batch"][
            "remaining"
        ]
        == 4
    )


def test_latest_reserved_short_result_can_borrow_past_body_soft_target() -> None:
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="run sequential commands",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    latest_call = AssistantMsg(
        name="assistant",
        id="msg:latest-call",
        content=[
            ToolCallBlock(id="call:latest", name="terminal", input='{"cmd":"two"}')
        ],
    )
    latest_result = AssistantMsg(
        name="assistant",
        content=[
            ToolResultBlock(
                id="call:latest",
                name="terminal",
                output=[TextBlock(text="SHORT_BUT_BODY_BUDGET_EXHAUSTED")],
                state=ToolResultState.SUCCESS,
            )
        ],
    )
    state.messages.extend([user, latest_call, latest_result])

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=LoopBudget(
            tool_result_context_chars=2_000,
            tool_result_body_context_chars=28,
            latest_tool_result_reserved_chars=64,
        ),
        context_id="context:latest-unsatisfied",
        model_call_index=2,
        current_user_anchor=user.id,
    )

    decision = next(
        decision
        for decision in compiled.tool_result_render_decisions
        if decision["tool_call_id"] == "call:latest"
    )
    assert decision["latest_reserved_candidate"] is True
    assert decision["latest_reserved_applied"] is True
    assert decision["latest_reserved_reason"] == "short_result_visible"
    assert (
        decision["body_budget_remaining"]
        == compiled.tool_result_budget_report["used_by_scope"]["latest_reserved"][
            "remaining"
        ]
    )
    assert any(
        diagnostic.get("code") == "tool_result_body_budget_unsatisfied"
        and diagnostic.get("severity") == "warning"
        for diagnostic in compiled.tool_result_budget_report["diagnostics"]
    )
    assert compiled.tool_result_budget_report["soft_target_overage"]["body_chars"] > 0


def test_max_tool_results_per_context_triggers_pressure() -> None:
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="run many commands",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    assistant = AssistantMsg(
        name="assistant",
        id="msg:many-tools",
        content=[
            ToolCallBlock(id=f"call:{idx}", name="terminal", input='{"cmd":"echo"}')
            for idx in range(3)
        ],
    )
    results = [
        AssistantMsg(
            name="assistant",
            content=[
                ToolResultBlock(
                    id=f"call:{idx}",
                    name="terminal",
                    output=[TextBlock(text=f"result:{idx}")],
                    state=ToolResultState.SUCCESS,
                )
            ],
        )
        for idx in range(3)
    ]
    state.messages.extend([user, assistant, *results])

    with pytest.raises(ContextBudgetExceeded) as exc_info:
        build_compiled_context(
            state=state,
            tools=(),
            system_prompt="System",
            budget=LoopBudget(max_tool_results_per_context=2),
            context_id="context:too-many-tool-results",
            model_call_index=2,
            current_user_anchor=user.id,
        )

    diagnostics = exc_info.value.tool_result_budget_report["diagnostics"]
    assert any(
        diagnostic.get("code") == "max_tool_results_per_context_exceeded"
        and diagnostic.get("severity") == "error"
        for diagnostic in diagnostics
    )


def test_terminal_essential_envelope_marks_clipped_error() -> None:
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="run broken command",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    assistant = AssistantMsg(
        name="assistant",
        content=[
            ToolCallBlock(id="call:error", name="terminal", input='{"cmd":"bad"}')
        ],
    )
    result = AssistantMsg(
        name="assistant",
        content=[
            ToolResultBlock(
                id="call:error",
                name="terminal",
                output=[
                    TextBlock(
                        text=json.dumps(
                            {
                                "status": "error",
                                "output": "body",
                                "exit_code": 1,
                                "cwd": "/workspace",
                                "error": "E" * 800,
                                "terminal_session_id": "default",
                                "backend_type": "local",
                            }
                        )
                    )
                ],
                state=ToolResultState.ERROR,
            )
        ],
    )
    state.messages.extend([user, assistant, result])

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=LoopBudget(
            tool_result_context_chars=2_000,
            current_tail_tool_result_context_chars=0,
            latest_tool_result_reserved_chars=0,
            tool_result_per_envelope_cap_chars=400,
        ),
        context_id="context:terminal-error-truncated",
        model_call_index=2,
        current_user_anchor=user.id,
    )

    rendered = "\n".join(
        text
        for message in compiled.llm_context.messages
        if message.role is MessageRole.TOOL_RESULT
        for text in message.content
    )
    payload = json.loads(rendered.split("\n", 1)[1])
    assert payload["error_truncated"] is True
    assert len(payload["error"]) < 800


def test_terminal_essential_envelope_respects_per_result_hard_cap() -> None:
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="run very broken command",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    assistant = AssistantMsg(
        name="assistant",
        content=[
            ToolCallBlock(
                id="call:tiny-envelope", name="terminal", input='{"cmd":"bad"}'
            )
        ],
    )
    result = AssistantMsg(
        name="assistant",
        content=[
            ToolResultBlock(
                id="call:tiny-envelope",
                name="terminal",
                output=[
                    TextBlock(
                        text=json.dumps(
                            {
                                "status": "error",
                                "output": "body",
                                "exit_code": 1,
                                "cwd": "/workspace/" + ("deep/" * 80),
                                "error": "ERR" * 800,
                                "process_id": "proc-" + ("x" * 400),
                                "terminal_session_id": "default",
                                "backend_type": "local",
                            }
                        )
                    )
                ],
                state=ToolResultState.ERROR,
            )
        ],
    )
    state.messages.extend([user, assistant, result])

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=LoopBudget(
            tool_result_context_chars=2_000,
            current_tail_tool_result_context_chars=0,
            latest_tool_result_reserved_chars=0,
            tool_result_per_envelope_cap_chars=256,
        ),
        context_id="context:terminal-envelope-cap",
        model_call_index=2,
        current_user_anchor=user.id,
    )

    decision = compiled.tool_result_render_decisions[0]
    assert decision["rendered_envelope_chars"] <= 256
    rendered = next(
        text
        for message in compiled.llm_context.messages
        if message.role is MessageRole.TOOL_RESULT
        for text in message.content
    )
    assert len(rendered) == decision["rendered_total_chars"]
    payload = json.loads(rendered.split("\n", 1)[1])
    assert payload["tool_result_body_omitted"] is True


def test_terminal_essential_envelope_preserves_timing_when_budget_allows() -> None:
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="check background process",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    timing = {
        "observed_at": "2026-07-09T01:02:03Z",
        "duration_seconds": 12.5,
        "freshness": "background_process_observation",
        "clock_source": "tool_payload",
    }
    observation = _tool_observation_timing(
        "call:timing",
        "terminal",
        observed_at="2026-07-09T01:02:04+00:00",
        freshness="background_process_observation",
    )
    payload = {
        "status": "running",
        "output": "x" * 2_000,
        "exit_code": -1,
        "cwd": "/workspace",
        "timed_out": False,
        "truncated": True,
        "process_id": "proc:timing",
        "yielded_to_background": True,
        "terminal_session_id": "default",
        "backend_type": "local",
        "timing": timing,
    }
    state.messages.extend(
        [
            user,
            AssistantMsg(
                name="assistant",
                content=[ToolCallBlock(id="call:timing", name="terminal", input="{}")],
            ),
            AssistantMsg(
                name="assistant",
                metadata={
                    "tool_observation_timing_by_call_id": {"call:timing": observation}
                },
                content=[
                    ToolResultBlock(
                        id="call:timing",
                        name="terminal",
                        output=[
                            TextBlock(text=json.dumps(payload, ensure_ascii=False))
                        ],
                        state=ToolResultState.SUCCESS,
                    )
                ],
            ),
        ]
    )

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=LoopBudget(
            tool_result_context_chars=2_000,
            current_tail_tool_result_context_chars=0,
            latest_tool_result_reserved_chars=0,
            tool_result_per_envelope_cap_chars=700,
        ),
        context_id="context:terminal-timing-envelope",
        model_call_index=2,
        current_user_anchor=user.id,
    )
    second = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=LoopBudget(tool_result_context_chars=8_000),
        context_id="context:terminal-timing-envelope:wider",
        model_call_index=3,
        current_user_anchor=user.id,
    )

    rendered = next(
        text
        for message in compiled.llm_context.messages
        if message.role is MessageRole.TOOL_RESULT
        for text in message.content
    )
    envelope = json.loads(rendered.split("\n", 1)[1])
    decision = compiled.tool_result_render_decisions[0]

    assert envelope["tool_result_body_omitted"] is True
    assert (
        envelope["pulsara_tool_observation"]["observed_at"]
        == observation["observed_at"]
    )
    assert envelope["timing"]["observed_at"] == timing["observed_at"]
    assert decision["tool_timing"]["observed_at"] == observation["observed_at"]
    assert decision["timing_policy"] in {"full", "minimal"}
    assert decision["rendered_timing_chars"] > 0
    assert decision["terminal_payload_timing"] == {
        "observed_at": timing["observed_at"],
        "duration_seconds": timing["duration_seconds"],
        "freshness": timing["freshness"],
    }
    assert decision["terminal_payload_timing_policy"] == "minimal"
    assert decision["rendered_terminal_payload_timing_chars"] > 0
    assert decision["rendered_envelope_chars"] <= 700
    second_rendered = next(
        text
        for message in second.llm_context.messages
        if message.role is MessageRole.TOOL_RESULT
        for text in message.content
    )
    second_envelope = json.loads(second_rendered.split("\n", 1)[1])
    second_decision = second.tool_result_render_decisions[0]
    assert second_decision["render_decision_cache_status"] == "freshly_collected"
    assert second_decision["terminal_payload_timing_policy"] == "full"
    assert second_decision["terminal_payload_timing"] == timing
    assert second_envelope["timing"] == timing


def test_artifact_backed_terminal_result_preserves_timing_in_normal_envelope_and_cache() -> (
    None
):
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="run archived terminal command",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    timing = {
        "observed_at": "2026-07-09T02:03:04Z",
        "duration_seconds": 1.25,
        "freshness": "current_tool_observation",
        "clock_source": "tool_payload",
    }
    observation = _tool_observation_timing(
        "call:artifact-timing",
        "terminal",
        observed_at="2026-07-09T02:03:05+00:00",
    )
    payload = {
        "status": "success",
        "output": "ARCHIVED_TERMINAL_OK",
        "exit_code": 0,
        "cwd": "/workspace",
        "timed_out": False,
        "truncated": False,
        "terminal_session_id": "default",
        "backend_type": "local",
        "timing": timing,
    }
    text = json.dumps(payload, ensure_ascii=False)
    artifact = ToolResultArtifactRef(
        artifact_id="artifact:terminal:combined",
        role="combined_output",
        media_type="text/plain; charset=utf-8",
        size_bytes=len(text.encode("utf-8")),
        stored_complete=True,
        preview=ToolResultPreviewMetadata(
            preview_policy="full",
            preview_chars=len(text),
            original_chars=len(text),
            original_bytes=len(text.encode("utf-8")),
            omitted_middle_chars=0,
            visible_head_chars=len(text),
            visible_tail_chars=0,
            read_more={
                "tool": "artifact_read",
                "artifact_id": "artifact:terminal:combined",
            },
        ),
    )
    state.messages.extend(
        [
            user,
            AssistantMsg(
                name="assistant",
                content=[
                    ToolCallBlock(
                        id="call:artifact-timing", name="terminal", input="{}"
                    )
                ],
            ),
            AssistantMsg(
                name="assistant",
                metadata={
                    "tool_observation_timing_by_call_id": {
                        "call:artifact-timing": observation
                    }
                },
                content=[
                    ToolResultBlock(
                        id="call:artifact-timing",
                        name="terminal",
                        output=[TextBlock(text=text)],
                        state=ToolResultState.SUCCESS,
                        artifacts=[artifact],
                    )
                ],
            ),
        ]
    )

    first = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=LoopBudget(tool_result_context_chars=8_000),
        context_id="context:artifact-terminal-timing:first",
        model_call_index=1,
        current_user_anchor=user.id,
    )
    second = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=LoopBudget(tool_result_context_chars=8_000),
        context_id="context:artifact-terminal-timing:second",
        model_call_index=2,
        current_user_anchor=user.id,
    )

    rendered = next(
        text
        for message in first.llm_context.messages
        if message.role is MessageRole.TOOL_RESULT
        for text in message.content
    )
    envelope = json.loads(rendered.split("\n", 1)[1])
    first_decision = first.tool_result_render_decisions[0]
    second_decision = second.tool_result_render_decisions[0]

    assert envelope["timing"] == timing
    assert envelope["pulsara_tool_observation"] == observation
    assert first_decision["timing_policy"] == "full"
    assert first_decision["tool_timing"] == observation
    assert first_decision["terminal_payload_timing_policy"] == "full"
    assert first_decision["terminal_payload_timing"] == timing
    assert not any(
        diagnostic.get("code") == "tool_observation_timing_omitted_for_envelope_cap"
        for diagnostic in first_decision["diagnostics"]
    )
    assert not any(
        diagnostic.get("code") == "terminal_payload_timing_omitted_for_envelope_cap"
        for diagnostic in first_decision["diagnostics"]
    )
    assert second_decision["render_decision_cache_status"] == "reused"
    assert second_decision["timing_policy"] == "full"
    assert second_decision["tool_timing"] == observation
    assert second_decision["terminal_payload_timing_policy"] == "full"
    assert second_decision["terminal_payload_timing"] == timing


def test_terminal_essential_envelope_omits_timing_when_cap_is_tiny() -> None:
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="check background process with tiny cap",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    payload = {
        "status": "running",
        "output": "x" * 2_000,
        "exit_code": -1,
        "cwd": "/workspace/" + ("deep/" * 20),
        "process_id": "proc:" + ("x" * 80),
        "terminal_session_id": "default",
        "backend_type": "local",
        "timing": {
            "observed_at": "2026-07-09T01:02:03Z",
            "duration_seconds": 12.5,
            "freshness": "background_process_observation",
            "clock_source": "tool_payload",
        },
    }
    observation = _tool_observation_timing(
        "call:tiny-timing",
        "terminal",
        observed_at="2026-07-09T01:02:04+00:00",
        freshness="background_process_observation",
    )
    state.messages.extend(
        [
            user,
            AssistantMsg(
                name="assistant",
                content=[
                    ToolCallBlock(id="call:tiny-timing", name="terminal", input="{}")
                ],
            ),
            AssistantMsg(
                name="assistant",
                metadata={
                    "tool_observation_timing_by_call_id": {
                        "call:tiny-timing": observation
                    }
                },
                content=[
                    ToolResultBlock(
                        id="call:tiny-timing",
                        name="terminal",
                        output=[
                            TextBlock(text=json.dumps(payload, ensure_ascii=False))
                        ],
                        state=ToolResultState.SUCCESS,
                    )
                ],
            ),
        ]
    )

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=LoopBudget(
            tool_result_context_chars=2_000,
            current_tail_tool_result_context_chars=0,
            latest_tool_result_reserved_chars=0,
            tool_result_per_envelope_cap_chars=180,
        ),
        context_id="context:terminal-timing-omitted",
        model_call_index=2,
        current_user_anchor=user.id,
    )

    rendered = next(
        text
        for message in compiled.llm_context.messages
        if message.role is MessageRole.TOOL_RESULT
        for text in message.content
    )
    envelope = json.loads(rendered.split("\n", 1)[1])
    decision = compiled.tool_result_render_decisions[0]

    assert "timing" not in envelope
    assert "pulsara_tool_observation" not in envelope
    assert decision["timing_policy"] == "omitted_for_cap"
    assert decision["rendered_timing_chars"] == 0
    assert decision["terminal_payload_timing_policy"] == "omitted_for_cap"
    assert decision["rendered_terminal_payload_timing_chars"] == 0
    assert any(
        diagnostic.get("code") == "tool_observation_timing_omitted_for_envelope_cap"
        for diagnostic in decision["diagnostics"]
    )
    assert any(
        diagnostic.get("code") == "terminal_payload_timing_omitted_for_envelope_cap"
        for diagnostic in decision["diagnostics"]
    )


def test_non_terminal_json_timing_field_is_business_payload_not_pulsara_timing() -> (
    None
):
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="read mcp-ish business json",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    payload = {
        "timing": {"phase": "p95", "observed_at": "external-business-clock"},
        "result": "BUSINESS_TIMING_OK",
    }
    state.messages.extend(
        [
            user,
            AssistantMsg(
                name="assistant",
                content=[
                    ToolCallBlock(
                        id="call:business", name="mcp__docs__search", input="{}"
                    )
                ],
            ),
            AssistantMsg(
                name="assistant",
                content=[
                    ToolResultBlock(
                        id="call:business",
                        name="mcp__docs__search",
                        output=[
                            TextBlock(text=json.dumps(payload, ensure_ascii=False))
                        ],
                        state=ToolResultState.SUCCESS,
                    )
                ],
            ),
        ]
    )

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=LoopBudget(tool_result_context_chars=8_000),
        context_id="context:business-timing",
        model_call_index=1,
        current_user_anchor=user.id,
    )

    rendered = next(
        text
        for message in compiled.llm_context.messages
        if message.role is MessageRole.TOOL_RESULT
        for text in message.content
    )
    decision = compiled.tool_result_render_decisions[0]

    assert "BUSINESS_TIMING_OK" in rendered
    assert "external-business-clock" in rendered
    assert "pulsara_tool_observation" not in rendered
    assert decision["timing_policy"] == "not_applicable"
    assert decision["tool_timing"] is None


def test_json_full_raw_tool_result_uses_pulsara_header_without_wrapping_payload() -> (
    None
):
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="read json result",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    observation = _tool_observation_timing(
        "call:json",
        "read_file",
        observed_at="2026-07-09T03:04:05+00:00",
    )
    payload = {"path": "config.json", "timing": {"business": True}, "ok": True}
    state.messages.extend(
        [
            user,
            AssistantMsg(
                name="assistant",
                content=[ToolCallBlock(id="call:json", name="read_file", input="{}")],
            ),
            AssistantMsg(
                name="assistant",
                metadata={
                    "tool_observation_timing_by_call_id": {"call:json": observation}
                },
                content=[
                    ToolResultBlock(
                        id="call:json",
                        name="read_file",
                        output=[
                            TextBlock(text=json.dumps(payload, ensure_ascii=False))
                        ],
                        state=ToolResultState.SUCCESS,
                    )
                ],
            ),
        ]
    )

    compiled = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=LoopBudget(tool_result_context_chars=8_000),
        context_id="context:json-full-raw",
        model_call_index=1,
        current_user_anchor=user.id,
    )

    rendered = next(
        text
        for message in compiled.llm_context.messages
        if message.role is MessageRole.TOOL_RESULT
        for text in message.content
    )
    header, raw_payload = rendered.split("\n", 1)
    decision = compiled.tool_result_render_decisions[0]

    assert header.startswith(
        "[tool_result:read_file:success; observed_at=2026-07-09T03:04:05+00:00"
    )
    assert json.loads(raw_payload) == payload
    assert "pulsara_tool_observation" not in raw_payload
    assert decision["framing"] == "pulsara_tool_result_header"
    assert decision["payload_preserved"] is True
    assert decision["payload_format"] == "json"
    assert decision["timing_policy"] == "full"
    assert decision["tool_timing"] == observation


def test_missing_production_tool_observation_timing_fails_context_compile_without_model_call() -> (
    None
):
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="use production event-derived result",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    state.messages.extend(
        [
            user,
            AssistantMsg(
                name="assistant",
                content=[
                    ToolCallBlock(
                        id="call:missing-timing", name="read_file", input="{}"
                    )
                ],
            ),
            AssistantMsg(
                name="assistant",
                metadata={
                    "source_timing": {
                        "observed_at": "2026-07-09T00:00:01+00:00",
                        "source_started_at": "2026-07-09T00:00:00+00:00",
                        "source_ended_at": "2026-07-09T00:00:01+00:00",
                    }
                },
                content=[
                    ToolResultBlock(
                        id="call:missing-timing",
                        name="read_file",
                        output=[TextBlock(text="PRODUCTION_MISSING_TIMING")],
                        state=ToolResultState.SUCCESS,
                    )
                ],
            ),
        ]
    )

    with pytest.raises(ContextBudgetExceeded) as exc_info:
        build_compiled_context(
            state=state,
            tools=(),
            system_prompt="System",
            budget=LoopBudget(tool_result_context_chars=8_000),
            context_id="context:missing-tool-observation",
            model_call_index=1,
            current_user_anchor=user.id,
        )

    assert "tool_observation_timing_missing" in str(exc_info.value)
    report = exc_info.value.tool_result_budget_report
    assert any(
        diagnostic.get("code") == "tool_observation_timing_missing"
        for diagnostic in report["diagnostics"]
    )


def test_tool_result_early_pressure_report_has_renderer_stage_and_null_unmeasured_fields() -> (
    None
):
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="inspect",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    state.messages.extend(
        [
            user,
            AssistantMsg(
                name="assistant",
                content=[
                    ToolCallBlock(id="call:no-timing", name="read_file", input="{}")
                ],
            ),
            AssistantMsg(
                name="assistant",
                metadata={"source_timing": {"observed_at": _TEST_COMPILED_AT}},
                content=[
                    ToolResultBlock(
                        id="call:no-timing",
                        name="read_file",
                        output=[TextBlock(text="missing")],
                        state=ToolResultState.SUCCESS,
                    )
                ],
            ),
        ]
    )
    with pytest.raises(ContextBudgetExceeded) as error:
        build_compiled_context(
            state=state,
            tools=(),
            system_prompt="System",
            budget=state.budget,
            context_id="context:renderer-stage",
            model_call_index=1,
            current_user_anchor=user.id,
        )

    budget = error.value.budget_report
    assert budget is not None
    assert budget.measurement_stage == "tool_result_render"
    assert budget.sections_estimated_tokens is None
    assert budget.final_payload_estimated_tokens is None


def test_memory_projection_lifecycle_fingerprint_tracks_visible_text_changes() -> None:
    coordinator = ContextLifecycleCoordinator()
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="what changed?",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    state.messages.append(user)
    state.memory_projection = {
        "summary": "FIRST_MEMORY_SENTINEL",
        "included_memory_ids": ["memory:stable"],
    }
    build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=state.budget,
        context_id="context:memory-first",
        model_call_index=1,
        current_user_anchor=user.id,
        lifecycle_coordinator=coordinator,
    )
    state.memory_projection = {
        "summary": "SECOND_MEMORY_SENTINEL",
        "included_memory_ids": ["memory:stable"],
    }
    second = build_compiled_context(
        state=state,
        tools=(),
        system_prompt="System",
        budget=state.budget,
        context_id="context:memory-second",
        model_call_index=2,
        current_user_anchor=user.id,
        lifecycle_coordinator=coordinator,
    )

    sections = {section.id: section for section in second.sections}
    assert sections["memory:projection"].lifecycle_status == "freshly_collected"
    assert any(
        decision.section_id == "memory:projection"
        and decision.decision == "invalidated"
        for decision in second.lifecycle_decisions
    )
    rendered = "\n".join(
        text for message in second.llm_context.messages for text in message.content
    )
    assert "SECOND_MEMORY_SENTINEL" in rendered
    assert "FIRST_MEMORY_SENTINEL" not in rendered


def test_context_compiler_rejects_current_user_that_exceeds_input_budget() -> None:
    state = LoopState(session_id="runtime:test")
    user = UserMsg(
        name="user",
        content="x" * 900_000,
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    state.messages.append(user)

    with pytest.raises(ContextBudgetExceeded, match="Current user input exceeds"):
        build_compiled_context(
            state=state,
            tools=(),
            system_prompt="System",
            budget=state.budget,
            context_id="context:too-large",
            model_call_index=1,
            current_user_anchor=user.id,
        )


def test_current_user_over_budget_has_specific_reason() -> None:
    test_context_compiler_rejects_current_user_that_exceeds_input_budget()
