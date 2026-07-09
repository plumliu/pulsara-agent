from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator
from uuid import uuid4

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    EventType,
    MemoryWriteResultEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.event.candidates import PreferenceCandidate, ValidCandidatePayload
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.graph import InMemoryGraphStore
from pulsara_agent.llm import LLMConfig, LLMRuntime, ModelProfile
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.memory import InMemoryArchiveStore
from pulsara_agent.memory.candidates.pool import (
    CandidateOrigin,
    InMemoryCandidatePool,
    MemoryGovernanceDecisionRecord,
    PooledMemoryCandidate,
    SubmitAsIsDecision,
    WriteFailedOutcome,
)
from pulsara_agent.memory.governance.executor import MemoryGovernanceExecutor
from pulsara_agent.memory.governance.engine import (
    MemoryGovernanceEngine,
    MemoryGovernanceOptions,
    _legacy_execution_context,
    _parse_governance_output,
    _related_existing_memories,
)
from pulsara_agent.memory.governance.relatedness import RelatednessAvailability
from pulsara_agent.memory.canonical.ledger import ExecutionEvidenceLedger
from pulsara_agent.memory.canonical.write_gate import MemoryWriteGate
from pulsara_agent.memory.canonical.write_service import MemoryWriteService
from pulsara_agent.message import ToolResultState
from pulsara_agent.ontology import memory
from tests.support.memory_uow import fake_memory_uow_factory


class _ScriptedTransport:
    api = "scripted"

    def __init__(self, replies: list[str]) -> None:
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
        text = self.replies.pop(0)
        yield ModelCallStartEvent(
            **event_context.event_fields(),
            model_name=model.id,
            model_role=model.role.value,
            provider=model.provider,
        )
        yield TextBlockStartEvent(**event_context.event_fields(), block_id="text:1")
        yield TextBlockDeltaEvent(**event_context.event_fields(), block_id="text:1", delta=text)
        yield TextBlockEndEvent(**event_context.event_fields(), block_id="text:1")
        yield ModelCallEndEvent(**event_context.event_fields())


def test_memory_governance_engine_submits_pending_candidate_with_synthetic_context() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    log = InMemoryEventLog()
    candidate = pool.append_candidate(_pooled_preference())
    transport = _ScriptedTransport(
        [
            json.dumps(
                {
                    "reason": "Durable explicit preference.",
                    "decisions": [
                        {
                            "kind": "submit_as_is",
                            "target_entry_id": candidate.entry_id,
                            "reason": "The candidate is a durable preference.",
                        }
                    ],
                }
            )
        ]
    )
    engine = MemoryGovernanceEngine(
        llm_runtime=_llm_runtime(transport),
        executor=_executor(pool=pool, graph=graph, log=log),
        options=MemoryGovernanceOptions(limit=5),
    )

    result = asyncio.run(
        engine.run_pending(
            trigger_reason="test",
            governance_batch_id="governance:test:engine",
        )
    )

    assert result.error_type is None
    assert [decision.kind for decision in result.decisions] == ["submit_as_is"]
    assert len(result.applied) == 1
    assert [event.type for event in result.applied[0].events] == [
        EventType.MEMORY_CANDIDATE_PROPOSED,
        EventType.MEMORY_WRITE_RESULT,
    ]
    assert all(event.run_id == "run:governance/governance:test:engine" for event in result.applied[0].events)
    assert pool.list_pending() == []
    assert len(graph.find_by_type(memory.PREFERENCE)) == 1
    assert "Memory Governance Agent" in transport.contexts[0].system_prompt


def test_memory_governance_engine_exposes_whole_batch_and_merges_before_apply() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    log = InMemoryEventLog()
    first = pool.append_candidate(
        _pooled_preference(statement="The user prefers concise summaries.")
    )
    second = pool.append_candidate(
        _pooled_preference(statement="The user prefers brief summaries.")
    )
    transport = _ScriptedTransport(
        [
            json.dumps(
                {
                    "reason": "The two pending candidates express one preference.",
                    "decisions": [
                        {
                            "kind": "merge_and_submit",
                            "target_entry_ids": [first.entry_id, second.entry_id],
                            "candidate": {
                                "kind": "Preference",
                                "candidate_id": "candidate:merged-concise-summary",
                                "statement": "The user prefers concise summaries.",
                                "scope": "ctx:user",
                                "source_authority": "explicit_user_instruction",
                                "verification_status": "user_confirmed",
                                "evidence_ids": [],
                            },
                            "reason": "Merge equivalent wording before either candidate is applied.",
                        }
                    ],
                }
            )
        ]
    )
    engine = MemoryGovernanceEngine(
        llm_runtime=_llm_runtime(transport),
        executor=_executor(pool=pool, graph=graph, log=log),
    )

    result = asyncio.run(
        engine.run_pending(
            trigger_reason="test",
            governance_batch_id="governance:test:whole-batch-merge",
        )
    )

    planner_payload = json.loads(
        transport.contexts[0].messages[0].content[0].split("\n", 1)[1]
    )
    assert [item["entry_id"] for item in planner_payload["candidates"]] == [
        first.entry_id,
        second.entry_id,
    ]
    assert [decision.kind for decision in result.decisions] == ["merge_and_submit"]
    assert result.error_type is None
    assert pool.list_pending() == []
    memories = graph.find_by_type(memory.PREFERENCE)
    assert len(memories) == 1
    assert memories[0][memory.STATEMENT.name] == "The user prefers concise summaries."


def test_memory_governance_engine_invalid_json_does_not_write_or_decide() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    log = InMemoryEventLog()
    pool.append_candidate(_pooled_preference())
    transport = _ScriptedTransport(["not json"])
    engine = MemoryGovernanceEngine(
        llm_runtime=_llm_runtime(transport),
        executor=_executor(pool=pool, graph=graph, log=log),
    )

    result = asyncio.run(engine.run_pending(trigger_reason="test"))

    assert result.error_type == "ValueError"
    assert result.applied == []
    assert pool.list_decisions() == []
    assert len(pool.list_pending()) == 1
    assert graph.find_by_type(memory.PREFERENCE) == []


def test_governance_compaction_candidate_uses_bounded_window_evidence_view() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    log = InMemoryEventLog()
    source_ctx = EventContext(run_id="run:source:compaction", turn_id="turn:source:compaction", reply_id="reply:source:compaction")
    first = log.append(TextBlockDeltaEvent(**source_ctx.event_fields(), block_id="text:one", delta="The user asks to sync release."))
    log.append(TextBlockDeltaEvent(**source_ctx.event_fields(), block_id="text:two", delta="The user asks to push GitHub."))
    candidate = pool.append_candidate(
        _pooled_preference(
            source_run_id="run:compaction:attribution",
            statement="The user prefers syncing release before pushing GitHub in this workspace.",
        ).model_copy(
            update={
                "origin": CandidateOrigin.COMPACTION,
                "source_event_id": "event:compaction-completed",
                "source_artifact_id": "context_compaction:test:summary",
                "intent_fingerprint": "sha256:compaction",
                "metadata": {
                    "compaction_id": "context_compaction:test",
                    "summary_artifact_id": "context_compaction:test:summary",
                    "summary_excerpt": "The user repeatedly asks to sync release before pushing.",
                    "summary_excerpt_chars": 62,
                    "summary_excerpt_truncated": False,
                    "included_run_ids": [source_ctx.run_id],
                    "included_run_count": 1,
                    "through_sequence": first.sequence,
                    "keep_after_sequence": first.sequence,
                    "source_event_id": "event:compaction-completed",
                    "source_event_sequence": 99,
                },
            }
        )
    )
    engine = MemoryGovernanceEngine(
        llm_runtime=_llm_runtime(_ScriptedTransport([])),
        executor=_executor(pool=pool, graph=graph, log=log),
    )

    snapshot = engine._candidate_snapshot(candidate)

    assert snapshot["origin"] == "compaction"
    assert snapshot["metadata"]["summary_excerpt"].startswith("The user repeatedly")
    assert snapshot["attribution_context"]["source_run_id"] == "run:compaction:attribution"
    evidence_view = snapshot["compaction_evidence_view"]
    assert evidence_view["compaction_id"] == "context_compaction:test"
    assert evidence_view["summary_artifact_id"] == "context_compaction:test:summary"
    assert evidence_view["summary_excerpt"].startswith("The user repeatedly")
    assert evidence_view["included_run_ids"] == [source_ctx.run_id]
    assert evidence_view["source_event_count"] == 1
    assert [item["event_id"] for item in snapshot["source_events"]] == [first.id]


def test_memory_governance_parser_accepts_contradict_and_submit() -> None:
    output = _parse_governance_output(
        json.dumps(
            {
                "reason": "Non-explicit same-subject preference conflict.",
                "decisions": [
                    {
                        "kind": "contradict_and_submit",
                        "target_entry_id": "pool:new",
                        "candidate": {
                            "kind": "Preference",
                            "candidate_id": "candidate:hate-egg-tarts",
                            "statement": "The user hates egg tarts.",
                            "scope": "ctx:user",
                            "source_authority": "explicit_user_instruction",
                            "verification_status": "user_confirmed",
                            "evidence_ids": [],
                        },
                        "contradicted_memory_ids": ["preference:likes-egg-tarts"],
                        "reason": "The new preference conflicts with the existing preference.",
                    }
                ],
            }
        )
    )

    assert [decision.kind for decision in output.decisions] == ["contradict_and_submit"]
    assert output.decisions[0].contradicted_memory_ids == ("preference:likes-egg-tarts",)


def test_memory_governance_engine_invalid_target_returns_error_without_write() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    log = InMemoryEventLog()
    pool.append_candidate(_pooled_preference())
    transport = _ScriptedTransport(
        [
            json.dumps(
                {
                    "reason": "Bad target id.",
                    "decisions": [
                        {
                            "kind": "submit_as_is",
                            "target_entry_id": "pool:missing",
                            "reason": "This id was not present in the input.",
                        }
                    ],
                }
            )
        ]
    )
    engine = MemoryGovernanceEngine(
        llm_runtime=_llm_runtime(transport),
        executor=_executor(pool=pool, graph=graph, log=log),
    )

    result = asyncio.run(
        engine.run_pending(
            trigger_reason="test",
            governance_batch_id="governance:test:bad-target",
        )
    )

    assert result.error_type == "KeyError"
    assert result.applied == []
    assert pool.list_decisions() == []
    assert len(pool.list_pending()) == 1
    assert log.iter(run_id="run:governance/governance:test:bad-target") == []
    assert graph.find_by_type(memory.PREFERENCE) == []


def test_memory_governance_engine_input_includes_candidate_audit_view() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    log = InMemoryEventLog()
    candidate = pool.append_candidate(_pooled_preference(source_run_id="run:source:audit"))
    assert isinstance(candidate.payload, ValidCandidatePayload)
    existing = candidate.payload.candidate.model_copy(update={"candidate_id": "candidate:existing"})
    existing_outcome = _service_on(graph).submit(
        existing,
        event_context=EventContext(run_id="run:old", turn_id="turn:old", reply_id="reply:old"),
    )
    assert any(isinstance(event, MemoryWriteResultEvent) for event in existing_outcome.events)
    log.extend(
        [
            ToolCallStartEvent(
                run_id="run:source:audit",
                turn_id=candidate.source_turn_id,
                reply_id=candidate.source_reply_id,
                tool_call_id="call:remember",
                tool_call_name="remember_preference",
            ),
            ToolCallDeltaEvent(
                run_id="run:source:audit",
                turn_id=candidate.source_turn_id,
                reply_id=candidate.source_reply_id,
                tool_call_id="call:remember",
                delta='{"statement":"The user prefers concise summaries."}',
            ),
            ToolCallEndEvent(
                run_id="run:source:audit",
                turn_id=candidate.source_turn_id,
                reply_id=candidate.source_reply_id,
                tool_call_id="call:remember",
            ),
            ToolResultStartEvent(
                run_id="run:source:audit",
                turn_id=candidate.source_turn_id,
                reply_id=candidate.source_reply_id,
                tool_call_id="call:remember",
                tool_call_name="remember_preference",
            ),
            ToolResultTextDeltaEvent(
                run_id="run:source:audit",
                turn_id=candidate.source_turn_id,
                reply_id=candidate.source_reply_id,
                tool_call_id="call:remember",
                delta='{"status":"proposed"}',
            ),
            ToolResultEndEvent(
                run_id="run:source:audit",
                turn_id=candidate.source_turn_id,
                reply_id=candidate.source_reply_id,
                tool_call_id="call:remember",
                state=ToolResultState.SUCCESS,
                metadata={"tool_observation_timing": {"observed_at": "2026-01-01T00:00:00Z"}},
            ),
        ]
    )
    pool.append_decision(
        MemoryGovernanceDecisionRecord(
            governance_batch_id="governance:test:previous-failure",
            decision=SubmitAsIsDecision(target_entry_id=candidate.entry_id, reason="previous try"),
            write_outcome=WriteFailedOutcome(
                error_type="RuntimeError",
                message="temporary store failure",
                write_event_ids=("event:failed",),
            ),
        )
    )
    transport = _ScriptedTransport(
        [
            json.dumps(
                {
                    "reason": "Duplicate existing memory.",
                    "decisions": [
                        {
                            "kind": "skip",
                            "target_entry_ids": [candidate.entry_id],
                            "reason": "Already present in canonical memory.",
                            "skip_reason": "duplicate_existing_memory",
                        }
                    ],
                }
            )
        ]
    )
    engine = MemoryGovernanceEngine(
        llm_runtime=_llm_runtime(transport),
        executor=_executor(pool=pool, graph=graph, log=log),
    )

    result = asyncio.run(
        engine.run_pending(
            trigger_reason="test",
            governance_batch_id="governance:test:audit-view",
        )
    )

    assert result.error_type is None
    user_payload = transport.contexts[0].messages[0].content[0].split("\n", 1)[1]
    governance_input = json.loads(user_payload)
    snapshot = governance_input["candidates"][0]
    assert snapshot["entry_id"] == candidate.entry_id
    assert snapshot["user_quote"] == "Please remember that I prefer concise summaries."
    assert snapshot["content_key"]
    assert any(event["tool_call_name"] == "remember_preference" for event in snapshot["source_events"])
    assert any("status" in event.get("delta", "") for event in snapshot["source_events"])
    assert snapshot["prior_governance_decisions"][0]["write_outcome"]["kind"] == "write_failed"
    assert snapshot["related_existing_memories"][0]["memory_id"]
    assert snapshot["related_existing_memories"][0]["is_exact_duplicate"] is True


def test_memory_governance_engine_empty_pool_does_not_call_llm() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    log = InMemoryEventLog()
    transport = _ScriptedTransport([])
    engine = MemoryGovernanceEngine(
        llm_runtime=_llm_runtime(transport),
        executor=_executor(pool=pool, graph=graph, log=log),
    )

    result = asyncio.run(engine.run_pending(trigger_reason="test"))

    assert result.decisions == []
    assert result.applied == []
    assert transport.contexts == []


def test_related_existing_memories_returns_active_same_scope_type_ranked_and_marks_duplicates() -> None:
    graph = InMemoryGraphStore()
    service = _service_on(graph)
    service.submit(
        _preference("candidate:dup", "The user prefers concise summaries."),
        event_context=EventContext(run_id="run:dup", turn_id="turn:dup", reply_id="reply:dup"),
    )
    service.submit(
        _preference("candidate:unrelated", "The user likes dark mode in the IDE."),
        event_context=EventContext(run_id="run:un", turn_id="turn:un", reply_id="reply:un"),
    )
    candidate = _pooled_preference()

    matches = _related_existing_memories(candidate, graph, graph_id=None)

    statements = [match["statement"] for match in matches]
    assert statements == [
        "The user prefers concise summaries.",
        "The user likes dark mode in the IDE.",
    ]
    assert matches[0]["is_exact_duplicate"] is True
    assert matches[1]["is_exact_duplicate"] is False


def _preference(candidate_id: str, statement: str) -> PreferenceCandidate:
    return PreferenceCandidate(
        candidate_id=candidate_id,
        statement=statement,
        scope="ctx:user",
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
    )


def _executor(
    *,
    pool: InMemoryCandidatePool,
    graph: InMemoryGraphStore,
    log: InMemoryEventLog,
) -> MemoryGovernanceExecutor:
    ledger = ExecutionEvidenceLedger(
        graph=graph,
        archive=InMemoryArchiveStore(),
        gate=MemoryWriteGate(),
    )
    service = MemoryWriteService(ledger=ledger)
    return MemoryGovernanceExecutor(
        candidate_pool=pool,
        memory_write_service=service,
        event_log=log,
        graph=graph,
        runtime_session_id="runtime:test",
        memory_write_uow_factory=fake_memory_uow_factory(
            graph=graph,
            candidate_pool=pool,
            memory_write_service=service,
        ),
    )


def _service_on(graph: InMemoryGraphStore) -> MemoryWriteService:
    return MemoryWriteService(
        ledger=ExecutionEvidenceLedger(
            graph=graph,
            archive=InMemoryArchiveStore(),
            gate=MemoryWriteGate(),
        )
    )


def _pooled_preference(
    *,
    source_run_id: str | None = None,
    statement: str = "The user prefers concise summaries.",
) -> PooledMemoryCandidate:
    user_quote = (
        "Please remember that I prefer concise summaries."
        if statement == "The user prefers concise summaries."
        else f"Please remember: {statement}"
    )
    return PooledMemoryCandidate(
        entry_id=f"pool:test:{uuid4().hex}",
        payload=ValidCandidatePayload(
            candidate=PreferenceCandidate(
                candidate_id=f"candidate:test:{uuid4().hex}",
                statement=statement,
                scope="ctx:user",
                source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
                verification_status=memory.VerificationStatus.USER_CONFIRMED,
            )
        ),
        origin=CandidateOrigin.MAIN_AGENT_TOOL,
        source_session_id="runtime:test",
        source_run_id=source_run_id or f"run:source:{uuid4().hex}",
        source_turn_id=f"turn:source:{uuid4().hex}",
        source_reply_id=f"reply:source:{uuid4().hex}",
        user_quote=user_quote,
    )


def _llm_runtime(transport: _ScriptedTransport) -> LLMRuntime:
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


def test_legacy_execution_context_fails_closed_for_lifecycle() -> None:
    # No relatedness service is wired (in-memory test-double path). Even when a
    # snapshot carries token-overlap related_existing_memories, the legacy
    # context must NOT authorize a destructive lifecycle action: every entry is
    # UNAVAILABLE with an empty allowlist, so supersede/contradict are blocked
    # while the advisory ids remain visible to Flash via the snapshot itself.
    snapshots = [
        {
            "entry_id": "pool:a",
            "related_existing_memories": [
                {"memory_id": "preference:overlap-1"},
                {"memory_id": "preference:overlap-2"},
            ],
        },
        {"entry_id": "pool:b", "related_existing_memories": []},
    ]

    context = _legacy_execution_context("governance:test:legacy-fail-closed", snapshots)

    assert context.availability["pool:a"] is RelatednessAvailability.UNAVAILABLE
    assert context.availability["pool:b"] is RelatednessAvailability.UNAVAILABLE
    assert context.allowlists["pool:a"] == frozenset()
    assert context.allowlists["pool:b"] == frozenset()
    # Token-overlap ids must never grant lifecycle authority on the legacy path.
    assert not context.allows_lifecycle("pool:a", "preference:overlap-1")
