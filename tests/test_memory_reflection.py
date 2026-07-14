from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncIterator

import pytest

from tests.support.runtime_session import in_memory_runtime_session

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    EventType,
    MemoryReflectionCompletedEvent,
    MemoryReflectionFailedEvent,
    RunErrorEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from pulsara_agent.event.candidates import InvalidAttemptPayload, ValidCandidatePayload
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.graph import InMemoryGraphStore
from pulsara_agent.llm import LLMRuntime
from tests.support import (
    stream_agent_task,
    test_llm_config,
    test_model_limits,
    test_resolved_call_fact,
)
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.memory.candidates.pool import InMemoryCandidatePool
from pulsara_agent.memory.hooks.durable import ReflectiveMemoryHooks
import pulsara_agent.memory.reflection.engine as reflection_module
from pulsara_agent.memory.reflection.engine import (
    MemoryReflectionEngine,
    MemoryReflectionHint,
    MemoryReflectionOptions,
    cheap_memory_hints,
)
from pulsara_agent.message import TextBlock, ToolResultBlock, ToolResultState, UserMsg
from pulsara_agent.ontology import memory
from pulsara_agent.runtime import AgentRuntime, LoopState, LoopStatus
from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.primitives.model_call import ModelCallPurpose, ModelTokenUsageFact


class _ScriptedTransport:
    api = "scripted"
    binding_id = "test.scripted"
    contract_version = "v1"

    def __init__(self, replies: list[dict]) -> None:
        self.replies = replies
        self.contexts: list[LLMContext] = []

    async def stream(
        self,
        *,
        call,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[AgentEvent]:
        reported_model_id = call.target.fact.model_id
        self.contexts.append(context)
        reply = self.replies.pop(0)
        if "text" in reply:
            yield TextBlockStartEvent(
                **event_context.event_fields(), block_id=f"text:{len(self.contexts)}"
            )
            yield TextBlockDeltaEvent(
                **event_context.event_fields(),
                block_id=f"text:{len(self.contexts)}",
                delta=reply["text"],
            )
            yield TextBlockEndEvent(
                **event_context.event_fields(), block_id=f"text:{len(self.contexts)}"
            )
        for call in reply.get("tool_calls", []):
            yield ToolCallStartEvent(
                **event_context.event_fields(),
                tool_call_id=call["id"],
                tool_call_name=call["name"],
            )
            yield ToolCallDeltaEvent(
                **event_context.event_fields(),
                tool_call_id=call["id"],
                delta=call["arguments"],
            )
            yield ToolCallEndEvent(
                **event_context.event_fields(), tool_call_id=call["id"]
            )
        if "run_error" in reply:
            yield RunErrorEvent(
                **event_context.event_fields(),
                code="provider_error",
                message=reply["run_error"],
            )
        if "usage" in reply:
            yield TransportUsageReport(
                usage_status="reported",
                usage=ModelTokenUsageFact.model_validate(reply["usage"]),
                reported_model_id=reported_model_id,
            )


def test_memory_reflection_queues_preference_from_explicit_memory_signal() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    transport = _ScriptedTransport([{"text": _reflection_json()}])
    engine = _reflection(graph=graph, pool=pool, transport=transport)

    events = asyncio.run(
        engine.reflect(
            state=_state(),
            event_store=InMemoryEventLog(),
            trigger_reasons=["cheap_memory_hint"],
            cheap_hints=[_hint()],
            safe_point="on_session_end",
        )
    )

    assert [event.type for event in events] == [EventType.MEMORY_REFLECTION_COMPLETED]
    completed = events[0]
    assert isinstance(completed, MemoryReflectionCompletedEvent)
    assert completed.trigger_reasons == ["cheap_memory_hint"]
    assert completed.proposed_count == 1
    assert completed.written_count == 0
    assert completed.resolved_call.purpose is ModelCallPurpose.MEMORY_REFLECTION
    assert completed.usage_status == "missing"
    assert completed.usage is None
    assert completed.estimated_input_tokens > 0
    pending = pool.list_pending()
    assert len(pending) == 1
    assert isinstance(pending[0].payload, ValidCandidatePayload)
    assert pending[0].origin.value == "reflection"
    assert pending[0].payload.candidate.kind == "Preference"
    assert graph.find_by_type(memory.PREFERENCE) == []


def test_memory_reflection_does_not_call_flash_without_trigger() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    transport = _ScriptedTransport([{"text": _reflection_json()}])
    engine = _reflection(graph=graph, pool=pool, transport=transport)

    events = asyncio.run(
        engine.reflect(
            state=_state("What is the current status?"),
            event_store=InMemoryEventLog(),
        )
    )

    assert events == []
    assert transport.contexts == []
    assert pool.list_pending() == []


def test_memory_reflection_invalid_json_returns_failure_event() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    transport = _ScriptedTransport([{"text": "not json"}])
    engine = _reflection(graph=graph, pool=pool, transport=transport)

    events = asyncio.run(
        engine.reflect(
            state=_state(),
            event_store=InMemoryEventLog(),
            trigger_reasons=["cheap_memory_hint"],
            cheap_hints=[_hint()],
            safe_point="on_session_end",
        )
    )

    assert len(events) == 1
    assert isinstance(events[0], MemoryReflectionFailedEvent)
    assert events[0].error_type == "ValueError"
    assert events[0].trigger_reasons == ["cheap_memory_hint"]
    assert events[0].failure_stage == "output_parse"
    assert events[0].resolved_call is not None
    assert events[0].estimated_input_tokens is not None
    assert pool.list_pending() == []


def test_reflection_completed_carries_call_fact() -> None:
    test_memory_reflection_queues_preference_from_explicit_memory_signal()


def test_reflection_completed_carries_usage_and_estimated_input() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    usage = {
        "input_tokens": 91,
        "cached_input_tokens": None,
        "output_tokens": 9,
        "reasoning_output_tokens": 3,
        "total_tokens": 100,
    }
    transport = _ScriptedTransport([{"text": _reflection_json(), "usage": usage}])
    engine = _reflection(graph=graph, pool=pool, transport=transport)

    events = asyncio.run(
        engine.reflect(
            state=_state(),
            event_store=InMemoryEventLog(),
            trigger_reasons=["cheap_memory_hint"],
            cheap_hints=[_hint()],
        )
    )

    completed = events[0]
    assert isinstance(completed, MemoryReflectionCompletedEvent)
    assert completed.usage_status == "reported"
    assert completed.usage == ModelTokenUsageFact.model_validate(usage)
    assert completed.estimated_input_tokens > 0
    assert completed.reported_model_id == completed.resolved_call.target.model_id


def test_reflection_failed_after_resolution_carries_call_fact() -> None:
    test_memory_reflection_invalid_json_returns_failure_event()


def test_reflection_failed_before_resolution_allows_missing_call_fact(
    monkeypatch,
) -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    transport = _ScriptedTransport([])
    engine = MemoryReflectionEngine(
        llm_runtime=_make_llm_runtime(transport),
        candidate_pool=pool,
        graph=graph,
        options=MemoryReflectionOptions(
            llm_options=reflection_module.LLMOptions()
        ),
    )

    def fail_resolution(self, **_kwargs):
        raise RuntimeError("synthetic target resolution failure")

    monkeypatch.setattr(LLMRuntime, "resolve_target", fail_resolution)

    events = asyncio.run(
        engine.reflect(
            state=_state(),
            event_store=InMemoryEventLog(),
            trigger_reasons=["cheap_memory_hint"],
            cheap_hints=[_hint()],
        )
    )

    failed = events[0]
    assert isinstance(failed, MemoryReflectionFailedEvent)
    assert failed.failure_stage == "target_resolution"
    assert failed.resolved_call is None
    assert failed.estimated_input_tokens is None


def test_reflection_failure_stage_enforces_call_and_usage_fields() -> None:
    fields = {
        "run_id": "run:reflection-contract",
        "turn_id": "turn:reflection-contract",
        "reply_id": "reply:reflection-contract",
        "reflection_id": "reflection:contract",
        "trigger_reason": "test",
        "error_type": "SyntheticError",
        "message": "synthetic",
    }
    with pytest.raises(ValueError):
        MemoryReflectionFailedEvent(**fields, failure_stage="model_stream")
    with pytest.raises(ValueError):
        MemoryReflectionFailedEvent(
            **fields,
            failure_stage="model_stream",
            resolved_call=test_resolved_call_fact(
                purpose=ModelCallPurpose.MEMORY_REFLECTION
            ),
        )


def test_reflection_identity_validation_failure_allows_missing_input_estimate() -> None:
    event = MemoryReflectionFailedEvent(
        run_id="run:reflection-identity",
        turn_id="turn:reflection-identity",
        reply_id="reply:reflection-identity",
        reflection_id="reflection:identity",
        trigger_reason="test",
        error_type="ModelContextIdentityMismatch",
        message="identity mismatch",
        failure_stage="model_validation",
        resolved_call=test_resolved_call_fact(
            purpose=ModelCallPurpose.MEMORY_REFLECTION
        ),
    )
    assert event.estimated_input_tokens is None


def test_reflection_budget_validation_failure_requires_input_estimate() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    transport = _ScriptedTransport([])
    runtime = _make_llm_runtime(
        transport,
        flash_limits=test_model_limits(
            total_context_tokens=256,
            max_input_tokens=224,
            max_output_tokens=64,
            default_output_tokens=32,
            input_safety_margin_tokens=8,
        ),
    )
    engine = MemoryReflectionEngine(
        llm_runtime=runtime,
        candidate_pool=pool,
        graph=graph,
        runtime_session=in_memory_runtime_session(Path.cwd()),
        options=MemoryReflectionOptions(llm_options=LLMOptions()),
    )

    events = asyncio.run(
        engine.reflect(
            state=_state("remember " + "x" * 8_000),
            event_store=InMemoryEventLog(),
            trigger_reasons=["cheap_memory_hint"],
            cheap_hints=[_hint()],
        )
    )

    failed = events[0]
    assert isinstance(failed, MemoryReflectionFailedEvent)
    assert failed.failure_stage == "model_validation"
    assert failed.estimated_input_tokens is not None
    assert transport.contexts == []


def test_reflection_stream_and_later_failures_require_input_estimate() -> None:
    test_memory_reflection_invalid_json_returns_failure_event()


def test_reflection_run_error_drains_end_before_failed_event() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    usage = {
        "input_tokens": 15,
        "cached_input_tokens": None,
        "output_tokens": 1,
        "reasoning_output_tokens": None,
        "total_tokens": 16,
    }
    transport = _ScriptedTransport([{"run_error": "provider failed", "usage": usage}])
    engine = _reflection(graph=graph, pool=pool, transport=transport)

    events = asyncio.run(
        engine.reflect(
            state=_state(),
            event_store=InMemoryEventLog(),
            trigger_reasons=["cheap_memory_hint"],
            cheap_hints=[_hint()],
        )
    )

    failed = events[0]
    assert isinstance(failed, MemoryReflectionFailedEvent)
    assert failed.failure_stage == "model_stream"
    assert failed.usage_status == "reported"
    assert failed.usage == ModelTokenUsageFact.model_validate(usage)
    assert failed.estimated_input_tokens is not None


def test_reflection_oversized_context_fails_before_provider() -> None:
    test_reflection_budget_validation_failure_requires_input_estimate()


def test_memory_reflection_false_positive_records_decision_without_candidate() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    transport = _ScriptedTransport([{"text": _reflection_false_json()}])
    engine = _reflection(graph=graph, pool=pool, transport=transport)

    events = asyncio.run(
        engine.reflect(
            state=_state("以后再说吧，先看当前测试结果。"),
            event_store=InMemoryEventLog(),
            trigger_reasons=["cheap_memory_hint"],
            cheap_hints=[
                MemoryReflectionHint(
                    source="cheap_string_match",
                    reason="matched cheap signal",
                    signal="以后",
                    excerpt="以后再说吧，先看当前测试结果。",
                )
            ],
            safe_point="on_session_end",
        )
    )

    assert [event.type for event in events] == [EventType.MEMORY_REFLECTION_COMPLETED]
    completed = events[0]
    assert isinstance(completed, MemoryReflectionCompletedEvent)
    assert completed.should_reflect is False
    assert completed.proposed_count == 0
    assert completed.written_count == 0
    assert pool.list_pending() == []


def test_cheap_memory_hints_cover_preference_and_instruction_phrases() -> None:
    hints = cheap_memory_hints(
        "从现在开始请直接给结论；I usually prefer concise summaries, and for the record my favorite format is bullets."
    )

    assert [hint.signal for hint in hints] == [
        "从现在开始",
        "i usually",
        "prefer",
        "for the record",
        "my favorite",
    ]


def test_cheap_memory_hints_cover_negative_preferences_and_corrections() -> None:
    hints = cheap_memory_hints("我真的不喜欢花哨的比喻。我的意思是：请直接说工程事实。")

    assert [hint.signal for hint in hints] == ["我真的不喜欢", "我的意思是"]


def test_cheap_memory_hints_cover_colloquial_english_corrections() -> None:
    hints = cheap_memory_hints(
        "Please don't call it magic. What I meant was: use precise implementation terms."
    )

    assert [hint.signal for hint in hints] == ["please don't", "what i meant was"]


def test_cheap_memory_hints_prefer_specific_overlapping_signal() -> None:
    hints = cheap_memory_hints("不要忘记以后都用 uv run pytest。")

    assert [hint.signal for hint in hints] == ["不要忘记", "以后都"]
    assert "不要" not in [hint.signal for hint in hints]
    assert "以后" not in [hint.signal for hint in hints]


def test_agent_runtime_flash_reflection_queues_candidate_at_session_end(
    tmp_path: Path,
) -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    transport = _ScriptedTransport(
        [
            {"text": "I will remember that."},
            {"text": _reflection_json()},
        ]
    )
    agent = _agent_with_reflection(
        tmp_path, graph=graph, pool=pool, transport=transport
    )

    events = asyncio.run(
        _collect(agent, "Please remember that I prefer concise summaries.")
    )

    event_types = [event.type for event in events]
    assert EventType.MEMORY_REFLECTION_COMPLETED in event_types
    assert EventType.MEMORY_WRITE_RESULT not in event_types
    assert event_types[-1] is EventType.RUN_END
    assert len(pool.list_pending()) == 1
    assert graph.find_by_type(memory.PREFERENCE) == []


def test_agent_runtime_main_agent_memory_attempt_suppresses_cheap_hint_reflection(
    tmp_path: Path,
) -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    transport = _ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:remember",
                        "name": "remember_preference",
                        "arguments": json.dumps(
                            {
                                "statement": "The user prefers concise summaries",
                                "scope": "ctx:user",
                                "source_authority": "explicit_user_instruction",
                                "verification_status": "user_confirmed",
                            }
                        ),
                    }
                ]
            },
            {"text": "done"},
        ]
    )
    agent = _agent_with_reflection(
        tmp_path, graph=graph, pool=pool, transport=transport
    )

    events = asyncio.run(_collect(agent, "Remember that I prefer concise summaries."))

    assert not any(
        isinstance(event, MemoryReflectionCompletedEvent) for event in events
    )
    assert len(pool.list_pending()) == 1
    assert graph.find_by_type(memory.PREFERENCE) == []
    assert len(transport.contexts) == 2


def test_agent_runtime_finalized_invalid_memory_attempt_suppresses_cheap_hint_reflection(
    tmp_path: Path,
) -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    transport = _ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:bad",
                        "name": "remember_preference",
                        "arguments": json.dumps(
                            {
                                "statement": "The user prefers concise summaries",
                                "scope": "ctx:user",
                                "applies_when": "misplaced action-boundary field",
                                "source_authority": "explicit_user_instruction",
                                "verification_status": "user_confirmed",
                            }
                        ),
                    }
                ]
            },
            {"text": "done"},
        ]
    )
    agent = _agent_with_reflection(
        tmp_path, graph=graph, pool=pool, transport=transport
    )

    events = asyncio.run(_collect(agent, "Remember that I prefer concise summaries."))

    assert not any(
        isinstance(event, MemoryReflectionCompletedEvent) for event in events
    )
    pending = pool.list_pending()
    assert len(pending) == 1
    assert isinstance(pending[0].payload, InvalidAttemptPayload)
    assert pending[0].source_tool_call_id == "call:bad"
    assert graph.find_by_type(memory.PREFERENCE) == []
    assert len(transport.contexts) == 2


def test_agent_runtime_reflection_failure_does_not_fail_run(tmp_path: Path) -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    transport = _ScriptedTransport(
        [
            {"text": "ok"},
            {"text": "not json"},
        ]
    )
    agent = _agent_with_reflection(
        tmp_path, graph=graph, pool=pool, transport=transport
    )

    events = asyncio.run(
        _collect(agent, "Please remember that I prefer concise summaries.")
    )

    assert any(isinstance(event, MemoryReflectionFailedEvent) for event in events)
    assert not any(
        isinstance(event, RunErrorEvent) and event.code == "memory_hook_error"
        for event in events
    )
    assert events[-1].type is EventType.RUN_END
    assert events[-1].status == "finished"
    assert pool.list_pending() == []


def test_aborted_run_skips_memory_reflection(tmp_path: Path) -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    transport = _ScriptedTransport([{"text": _reflection_json()}])
    agent = _agent_with_reflection(
        tmp_path, graph=graph, pool=pool, transport=transport
    )
    state = LoopState(session_id=agent.runtime_session.runtime_session_id)
    state.status = LoopStatus.ABORTED
    state.stop_reason = "aborted"
    state.messages.append(
        UserMsg(name="user", content="Please remember that I prefer concise summaries.")
    )

    async def run():
        await agent.memory_hooks.on_session_start(
            state, "Please remember that I prefer concise summaries."
        )
        return await agent.memory_hooks.on_session_end(state)

    events = asyncio.run(run())

    assert events == []
    assert transport.contexts == []
    assert pool.list_pending() == []


def test_failed_run_skips_memory_reflection(tmp_path: Path) -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    transport = _ScriptedTransport([{"text": _reflection_json()}])
    agent = _agent_with_reflection(
        tmp_path, graph=graph, pool=pool, transport=transport
    )
    state = LoopState(session_id=agent.runtime_session.runtime_session_id)
    state.status = LoopStatus.FAILED
    state.stop_reason = "model_error"
    state.messages.append(
        UserMsg(name="user", content="Please remember that I prefer concise summaries.")
    )

    async def run():
        await agent.memory_hooks.on_session_start(
            state, "Please remember that I prefer concise summaries."
        )
        return await agent.memory_hooks.on_session_end(state)

    events = asyncio.run(run())

    assert events == []
    assert transport.contexts == []
    assert pool.list_pending() == []


def test_finished_run_still_allows_memory_reflection(tmp_path: Path) -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    transport = _ScriptedTransport([{"text": _reflection_json()}])
    agent = _agent_with_reflection(
        tmp_path, graph=graph, pool=pool, transport=transport
    )
    state = LoopState(session_id=agent.runtime_session.runtime_session_id)
    state.status = LoopStatus.FINISHED
    state.stop_reason = "final"
    state.messages.append(
        UserMsg(name="user", content="Please remember that I prefer concise summaries.")
    )

    async def run():
        await agent.memory_hooks.on_session_start(
            state, "Please remember that I prefer concise summaries."
        )
        return await agent.memory_hooks.on_session_end(state)

    events = asyncio.run(run())

    assert [event.type for event in events] == [EventType.MEMORY_REFLECTION_COMPLETED]
    assert len(transport.contexts) == 1
    assert len(pool.list_pending()) == 1


def test_prompt_contains_few_shots_and_cheap_hint_warning() -> None:
    prompt = reflection_module._REFLECTION_SYSTEM_PROMPT

    assert "Few-shot examples" in prompt
    assert "cheap hints are hints and may be false positives" in prompt
    assert "Example A: explicit durable preference" in prompt
    assert "Example B: cheap hint false positive" in prompt
    assert "Example C: main agent already wrote the memory" in prompt


def _reflection(
    *,
    graph: InMemoryGraphStore,
    pool: InMemoryCandidatePool,
    transport: _ScriptedTransport,
) -> MemoryReflectionEngine:
    return MemoryReflectionEngine(
        llm_runtime=_make_llm_runtime(transport),
        candidate_pool=pool,
        graph=graph,
        runtime_session=in_memory_runtime_session(Path.cwd()),
    )


def _agent_with_reflection(
    tmp_path: Path,
    *,
    graph: InMemoryGraphStore,
    pool: InMemoryCandidatePool,
    transport: _ScriptedTransport,
) -> AgentRuntime:
    runtime_session = in_memory_runtime_session(tmp_path)
    llm_runtime = _make_llm_runtime(transport)
    reflection = MemoryReflectionEngine(
        llm_runtime=llm_runtime,
        candidate_pool=pool,
        graph=graph,
        runtime_session=runtime_session,
    )
    hooks = ReflectiveMemoryHooks(
        candidate_pool=pool,
        sink=runtime_session.memory_proposal_sink,
        reflection=reflection,
        event_store=runtime_session.event_log,
    )
    return AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=llm_runtime,
        memory_hooks=hooks,
    )


def _make_llm_runtime(
    transport: _ScriptedTransport,
    *,
    flash_limits=None,
) -> LLMRuntime:
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        flash_limits=flash_limits,
        api="scripted",
    )
    registry = LLMTransportRegistry()
    registry.register(transport)
    return LLMRuntime(config=config, registry=registry)


def _state(
    user_text: str = "Please remember that I prefer concise summaries.",
) -> LoopState:
    state = LoopState(session_id="runtime:test")
    state.messages.append(UserMsg(name="user", content=user_text))
    return state


def _reflection_json(*, statement: str = "The user prefers concise summaries") -> str:
    return json.dumps(
        {
            "should_reflect": True,
            "reason": "The user explicitly asked to remember a durable preference.",
            "quoted_evidence": ["Please remember that I prefer concise summaries."],
            "candidate_kinds": ["Preference"],
            "summary": "found one durable preference",
            "candidates": [
                {
                    "kind": "Preference",
                    "statement": statement,
                    "scope": "ctx:user",
                    "source_authority": "explicit_user_instruction",
                    "verification_status": "user_confirmed",
                    "evidence_ids": [],
                }
            ],
            "skipped_candidates": [],
        }
    )


def _reflection_false_json() -> str:
    return json.dumps(
        {
            "should_reflect": False,
            "reason": "The cheap hint is a false positive.",
            "quoted_evidence": ["以后再说吧"],
            "candidate_kinds": [],
            "summary": "no durable memory",
            "candidates": [],
            "skipped_candidates": [{"reason": "cheap_hint_false_positive"}],
        }
    )


def _hint(signal: str = "remember") -> MemoryReflectionHint:
    return MemoryReflectionHint(
        source="cheap_string_match",
        reason="test hint",
        signal=signal,
        excerpt="Please remember that I prefer concise summaries.",
    )


async def _collect(agent: AgentRuntime, user_input: str) -> list[AgentEvent]:
    return [event async for event in stream_agent_task(agent, user_input)]


def _tool_results(count: int) -> list[ToolResultBlock]:
    return [
        ToolResultBlock(
            id=f"call:{index}",
            name="read_file",
            output=[TextBlock(text=f"tool result {index}")],
            state=ToolResultState.SUCCESS,
        )
        for index in range(count)
    ]
