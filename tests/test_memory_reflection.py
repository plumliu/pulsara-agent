from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncIterator

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    EventType,
    MemoryReflectionCompletedEvent,
    MemoryReflectionFailedEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
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
from pulsara_agent.llm import LLMConfig, LLMRuntime, ModelProfile
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.memory.candidates.pool import InMemoryCandidatePool
from pulsara_agent.memory.hooks.durable import ReflectiveMemoryHooks
import pulsara_agent.memory.reflection.engine as reflection_module
from pulsara_agent.memory.reflection.engine import MemoryReflectionEngine, MemoryReflectionHint, cheap_memory_hints
from pulsara_agent.message import TextBlock, ToolResultBlock, ToolResultState, UserMsg
from pulsara_agent.ontology import memory
from pulsara_agent.runtime import AgentRuntime, LoopState, LoopStatus, RuntimeSession


class _ScriptedTransport:
    api = "scripted"

    def __init__(self, replies: list[dict]) -> None:
        self.replies = replies
        self.contexts: list[LLMContext] = []

    async def stream(
        self,
        *,
        model: ModelProfile,
        context: LLMContext,
        event_context: EventContext,
        options: LLMOptions | None = None,
    ) -> AsyncIterator[AgentEvent]:
        self.contexts.append(context)
        reply = self.replies.pop(0)
        yield ModelCallStartEvent(
            **event_context.event_fields(),
            model_name=model.id,
            model_role=model.role.value,
            provider=model.provider,
        )
        if "text" in reply:
            yield TextBlockStartEvent(**event_context.event_fields(), block_id=f"text:{len(self.contexts)}")
            yield TextBlockDeltaEvent(
                **event_context.event_fields(),
                block_id=f"text:{len(self.contexts)}",
                delta=reply["text"],
            )
            yield TextBlockEndEvent(**event_context.event_fields(), block_id=f"text:{len(self.contexts)}")
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
            yield ToolCallEndEvent(**event_context.event_fields(), tool_call_id=call["id"])
        yield ModelCallEndEvent(**event_context.event_fields())


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
    assert pool.list_pending() == []


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

    assert [hint.signal for hint in hints] == ["从现在开始", "i usually", "prefer", "for the record", "my favorite"]


def test_cheap_memory_hints_cover_negative_preferences_and_corrections() -> None:
    hints = cheap_memory_hints("我真的不喜欢花哨的比喻。我的意思是：请直接说工程事实。")

    assert [hint.signal for hint in hints] == ["我真的不喜欢", "我的意思是"]


def test_cheap_memory_hints_cover_colloquial_english_corrections() -> None:
    hints = cheap_memory_hints("Please don't call it magic. What I meant was: use precise implementation terms.")

    assert [hint.signal for hint in hints] == ["please don't", "what i meant was"]


def test_cheap_memory_hints_prefer_specific_overlapping_signal() -> None:
    hints = cheap_memory_hints("不要忘记以后都用 uv run pytest。")

    assert [hint.signal for hint in hints] == ["不要忘记", "以后都"]
    assert "不要" not in [hint.signal for hint in hints]
    assert "以后" not in [hint.signal for hint in hints]


def test_agent_runtime_flash_reflection_queues_candidate_at_session_end(tmp_path: Path) -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    transport = _ScriptedTransport(
        [
            {"text": "I will remember that."},
            {"text": _reflection_json()},
        ]
    )
    agent = _agent_with_reflection(tmp_path, graph=graph, pool=pool, transport=transport)

    events = asyncio.run(_collect(agent, "Please remember that I prefer concise summaries."))

    event_types = [event.type for event in events]
    assert EventType.MEMORY_REFLECTION_COMPLETED in event_types
    assert EventType.MEMORY_WRITE_RESULT not in event_types
    assert event_types[-1] is EventType.RUN_END
    assert len(pool.list_pending()) == 1
    assert graph.find_by_type(memory.PREFERENCE) == []


def test_agent_runtime_main_agent_memory_attempt_suppresses_cheap_hint_reflection(tmp_path: Path) -> None:
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
    agent = _agent_with_reflection(tmp_path, graph=graph, pool=pool, transport=transport)

    events = asyncio.run(_collect(agent, "Remember that I prefer concise summaries."))

    assert not any(isinstance(event, MemoryReflectionCompletedEvent) for event in events)
    assert len(pool.list_pending()) == 1
    assert graph.find_by_type(memory.PREFERENCE) == []
    assert len(transport.contexts) == 2


def test_agent_runtime_finalized_invalid_memory_attempt_suppresses_cheap_hint_reflection(tmp_path: Path) -> None:
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
    agent = _agent_with_reflection(tmp_path, graph=graph, pool=pool, transport=transport)

    events = asyncio.run(_collect(agent, "Remember that I prefer concise summaries."))

    assert not any(isinstance(event, MemoryReflectionCompletedEvent) for event in events)
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
    agent = _agent_with_reflection(tmp_path, graph=graph, pool=pool, transport=transport)

    events = asyncio.run(_collect(agent, "Please remember that I prefer concise summaries."))

    assert any(isinstance(event, MemoryReflectionFailedEvent) for event in events)
    assert not any(isinstance(event, RunErrorEvent) and event.code == "memory_hook_error" for event in events)
    assert events[-1].type is EventType.RUN_END
    assert events[-1].status == "finished"
    assert pool.list_pending() == []


def test_aborted_run_skips_memory_reflection(tmp_path: Path) -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    transport = _ScriptedTransport([{"text": _reflection_json()}])
    agent = _agent_with_reflection(tmp_path, graph=graph, pool=pool, transport=transport)
    state = LoopState(session_id=agent.runtime_session.runtime_session_id)
    state.status = LoopStatus.ABORTED
    state.stop_reason = "aborted"
    state.messages.append(UserMsg(name="user", content="Please remember that I prefer concise summaries."))

    async def run():
        await agent.memory_hooks.on_session_start(state, "Please remember that I prefer concise summaries.")
        return await agent.memory_hooks.on_session_end(state)

    events = asyncio.run(run())

    assert events == []
    assert transport.contexts == []
    assert pool.list_pending() == []


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
    )


def _agent_with_reflection(
    tmp_path: Path,
    *,
    graph: InMemoryGraphStore,
    pool: InMemoryCandidatePool,
    transport: _ScriptedTransport,
) -> AgentRuntime:
    runtime_session = RuntimeSession(tmp_path)
    llm_runtime = _make_llm_runtime(transport)
    reflection = MemoryReflectionEngine(
        llm_runtime=llm_runtime,
        candidate_pool=pool,
        graph=graph,
    )
    hooks = ReflectiveMemoryHooks(
        candidate_pool=pool,
        sink=runtime_session.memory_proposal_sink,
        reflection=reflection,
        event_store=runtime_session.event_log,
    )
    return AgentRuntime(
        runtime_session=runtime_session,
        llm_runtime=llm_runtime,
        memory_hooks=hooks,
    )


def _make_llm_runtime(transport: _ScriptedTransport) -> LLMRuntime:
    config = LLMConfig(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api="scripted",
    )
    registry = LLMTransportRegistry()
    registry.register(transport)
    return LLMRuntime(config=config, registry=registry)


def _state(user_text: str = "Please remember that I prefer concise summaries.") -> LoopState:
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
    return [event async for event in agent.stream_task(user_input)]


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
