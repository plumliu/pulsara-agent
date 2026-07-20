from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import AsyncIterator, TypeAlias

from tests.support import test_llm_config
from tests.support.raw_provider import (
    RawProviderTextBlockEnd,
    RawProviderTextBlockStart,
    RawProviderTextDelta,
)

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    MemoryCandidateEvidenceRejectedEvent,
    MemoryGovernanceBatchCompletedEvent,
    MemoryGovernanceBatchFailedEvent,
    MemoryGovernanceBatchPreparedEvent,
    MemoryReflectionCompletedEvent,
    ModelCallStartEvent,
)
from pulsara_agent.llm import LLMRuntime
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.llm.user_carrier import RUNTIME_REQUEST_ENVELOPE_KEY
from pulsara_agent.memory.governance.engine import (
    MemoryGovernanceEngine,
    _parse_governance_output,
)
from pulsara_agent.memory.governance.preparation import (
    GovernanceBatchPreparationStatus,
)
from pulsara_agent.memory.reflection.engine import (
    MemoryReflectionEngine,
    MemoryReflectionHint,
)
from pulsara_agent.memory.scope import MemoryDomainContext
from pulsara_agent.message import UserMsg
from pulsara_agent.ontology import memory
from pulsara_agent.primitives.governance_evidence import (
    GovernanceCandidateClaimStatus,
)
from pulsara_agent.primitives.model_call import (
    ModelCallPurpose,
    ModelTokenUsageFact,
)
from pulsara_agent.runtime import LoopState
from pulsara_agent.runtime.wiring import build_in_memory_runtime_wiring


ReplyFactory: TypeAlias = Callable[[LLMContext], dict[str, object]]
ScriptedReply: TypeAlias = dict[str, object] | ReplyFactory


class _ScriptedTransport:
    api = "scripted"
    binding_id = "test.scripted"
    contract_version = "v1"

    def __init__(self, replies: list[ScriptedReply]) -> None:
        self.replies = replies
        self.contexts: list[LLMContext] = []

    async def stream(
        self,
        *,
        call,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[AgentEvent]:
        self.contexts.append(context)
        scripted = self.replies.pop(0)
        reply = scripted(context) if callable(scripted) else scripted
        text = str(reply.get("text", ""))
        block_id = f"text:{len(self.contexts)}"
        yield RawProviderTextBlockStart(
            **event_context.event_fields(),
            block_id=block_id,
        )
        yield RawProviderTextDelta(
            **event_context.event_fields(),
            block_id=block_id,
            delta=text,
        )
        yield RawProviderTextBlockEnd(
            **event_context.event_fields(),
            block_id=block_id,
        )
        usage = reply.get("usage")
        if usage is not None:
            yield TransportUsageReport(
                usage_status="reported",
                usage=ModelTokenUsageFact.model_validate(usage),
                reported_model_id=call.target.fact.model_id,
            )


def test_memory_governance_engine_runs_event_first_evidence_pipeline(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = _harness(
            tmp_path,
            [
                {"text": _reflection_json()},
                _submit_all_reply,
            ],
        )
        candidate_ids = await _produce_reflection_candidate(harness)

        result = await harness.engine.run_pending(
            trigger_reason="test",
            governance_batch_id="governance:test:event-first",
        )

        assert result.error_type is None
        assert [decision.kind for decision in result.decisions] == ["submit_as_is"]
        assert len(result.applied) == 1
        assert harness.wiring.candidate_pool.list_pending() == []
        memories = harness.wiring.graph.find_by_type(
            memory.PREFERENCE,
            graph_id=harness.wiring.graph_id,
        )
        assert len(memories) == 1
        assert memories[0][memory.STATEMENT.name] == (
            "The user prefers concise summaries."
        )

        events = harness.wiring.event_log.iter()
        prepared = tuple(
            event for event in events if isinstance(event, MemoryGovernanceBatchPreparedEvent)
        )
        completed = tuple(
            event for event in events if isinstance(event, MemoryGovernanceBatchCompletedEvent)
        )
        governance_starts = tuple(
            event
            for event in events
            if isinstance(event, ModelCallStartEvent)
            and event.resolved_call.purpose is ModelCallPurpose.MEMORY_GOVERNANCE
        )
        assert len(prepared) == len(completed) == len(governance_starts) == 1
        assert prepared[0].sequence is not None
        assert governance_starts[0].sequence is not None
        assert prepared[0].sequence < governance_starts[0].sequence
        assert prepared[0].candidate_entry_ids == candidate_ids
        assert completed[0].prepared_event_id == prepared[0].id
        attribution = governance_starts[0].governance_input_attribution
        assert attribution is not None
        assert attribution.batch_input_reference == prepared[0].batch_input_reference
        assert result.resolved_model_call == governance_starts[0].resolved_call

        claims = harness.wiring.memory_governance_claim_repository.claims_for_batch(
            runtime_session_id="runtime:test",
            governance_batch_id="governance:test:event-first",
        )
        assert {claim.status for claim in claims} == {
            GovernanceCandidateClaimStatus.TERMINAL
        }
        preparation = harness.wiring.memory_governance_preparation_repository.get(
            runtime_session_id="runtime:test",
            governance_batch_id="governance:test:event-first",
        )
        assert preparation is not None
        assert preparation.status is GovernanceBatchPreparationStatus.TERMINAL

        model_input = _governance_input(harness.transport.contexts[1])
        prompt_view = model_input["candidates"][0]
        prompt_candidate = prompt_view["candidate"]
        assert prompt_candidate["candidate_entry_id"] == candidate_ids[0]
        assert prompt_candidate["evidence_kind"] == "reflection"
        assert prompt_view["decision_candidate"]["statement"] == (
            "The user prefers concise summaries."
        )
        assert prompt_view["lifecycle"][
            "allowed_replacement_evidence_refs"
        ] == []
        assert "source_events" not in json.dumps(model_input, sort_keys=True)
        assert prompt_candidate["ordered_evidence_texts"][0]["field_code"] == (
            "reflection_report"
        )

    asyncio.run(scenario())


def test_memory_governance_claims_are_all_or_none_across_batches(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = _harness(
            tmp_path,
            [{"text": _reflection_json(two_candidates=True)}],
        )
        candidate_ids = await _produce_reflection_candidate(harness)
        repository = harness.wiring.memory_governance_claim_repository

        first, second = await asyncio.gather(
            asyncio.to_thread(
                repository.claim_pending_batch,
                runtime_session_id="runtime:test",
                governance_batch_id="governance:test:claim-a",
                limit=8,
            ),
            asyncio.to_thread(
                repository.claim_pending_batch,
                runtime_session_id="runtime:test",
                governance_batch_id="governance:test:claim-b",
                limit=8,
            ),
        )

        winners = tuple(batch for batch in (first, second) if batch.candidates)
        losers = tuple(batch for batch in (first, second) if not batch.candidates)
        assert len(winners) == len(losers) == 1
        assert tuple(item.entry_id for item in winners[0].candidates) == candidate_ids
        assert winners[0].claims
        assert losers[0].claims == ()

    asyncio.run(scenario())


def test_memory_governance_invalid_source_is_system_rejected(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = _harness(tmp_path, [{"text": _reflection_json()}])
        (candidate_id,) = await _produce_reflection_candidate(harness)
        pool = harness.wiring.candidate_pool
        stored = pool._candidates[candidate_id]
        pool._candidates[candidate_id] = stored.model_copy(
            update={"source_event_id": "memory_reflection:missing"},
            deep=True,
        )

        result = await harness.engine.run_pending(
            trigger_reason="invalid-source",
            governance_batch_id="governance:test:invalid-source",
        )

        assert result.error_type is None
        assert result.decisions == []
        assert harness.transport.contexts == [harness.transport.contexts[0]]
        rejections = tuple(
            event
            for event in harness.wiring.event_log.iter()
            if isinstance(event, MemoryCandidateEvidenceRejectedEvent)
        )
        assert len(rejections) == 1
        assert rejections[0].rejection.candidate_entry_id == candidate_id
        assert pool.evidence_rejection_event_id(candidate_id) == rejections[0].id
        assert pool.list_pending() == []
        claims = harness.wiring.memory_governance_claim_repository.claims_for_batch(
            runtime_session_id="runtime:test",
            governance_batch_id="governance:test:invalid-source",
        )
        assert {claim.status for claim in claims} == {
            GovernanceCandidateClaimStatus.TERMINAL
        }

    asyncio.run(scenario())


def test_memory_governance_rejects_invalid_sibling_and_prepares_valid_one(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = _harness(
            tmp_path,
            [
                {"text": _reflection_json(two_candidates=True)},
                _submit_all_reply,
            ],
        )
        candidate_ids = await _produce_reflection_candidate(harness)
        invalid_id, valid_id = candidate_ids
        pool = harness.wiring.candidate_pool
        invalid = pool._candidates[invalid_id]
        pool._candidates[invalid_id] = invalid.model_copy(
            update={"source_event_id": "memory_reflection:missing"},
            deep=True,
        )
        batch_id = "governance:test:mixed-validity"

        result = await harness.engine.run_pending(
            trigger_reason="mixed-validity",
            governance_batch_id=batch_id,
        )

        assert result.error_type is None
        assert len(result.applied) == 1
        assert result.decisions[0].target_entry_id == valid_id
        rejections = tuple(
            event
            for event in harness.wiring.event_log.iter()
            if isinstance(event, MemoryCandidateEvidenceRejectedEvent)
        )
        assert len(rejections) == 1
        assert rejections[0].rejection.candidate_entry_id == invalid_id
        claims = harness.wiring.memory_governance_claim_repository.claims_for_batch(
            runtime_session_id="runtime:test",
            governance_batch_id=batch_id,
        )
        assert {claim.status for claim in claims} == {
            GovernanceCandidateClaimStatus.TERMINAL
        }
        prepared = next(
            event
            for event in harness.wiring.event_log.iter()
            if isinstance(event, MemoryGovernanceBatchPreparedEvent)
        )
        assert prepared.candidate_entry_ids == (valid_id,)
        assert pool.list_pending() == []

    asyncio.run(scenario())


def test_memory_governance_prepared_recovery_uses_frozen_call(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def scenario() -> None:
        harness = _harness(
            tmp_path,
            [
                {"text": _reflection_json()},
                _submit_all_reply,
            ],
        )
        await _produce_reflection_candidate(harness)
        batch_id = "governance:test:frozen-prepared-recovery"
        original_call_or_materialize = MemoryGovernanceEngine._call_or_materialize

        async def leave_model_stream_unstarted(self, **kwargs):
            del self, kwargs
            return None

        monkeypatch.setattr(
            MemoryGovernanceEngine,
            "_call_or_materialize",
            leave_model_stream_unstarted,
        )
        first = await harness.engine.run_pending(
            trigger_reason="prepare-only",
            governance_batch_id=batch_id,
        )
        assert first.error_type == "GovernanceModelStreamNotTerminal"
        prepared = tuple(
            event
            for event in harness.wiring.event_log.iter()
            if isinstance(event, MemoryGovernanceBatchPreparedEvent)
        )
        assert len(prepared) == 1
        frozen_call_id = prepared[0].resolved_model_call_id

        monkeypatch.setattr(
            MemoryGovernanceEngine,
            "_call_or_materialize",
            original_call_or_materialize,
        )

        def unexpected_resolve(*args, **kwargs):
            del args, kwargs
            raise AssertionError("Prepared recovery must not resolve current config")

        monkeypatch.setattr(LLMRuntime, "resolve_target", unexpected_resolve)
        monkeypatch.setattr(LLMRuntime, "resolve_call", unexpected_resolve)
        second = await harness.engine.run_pending(
            trigger_reason="recovery",
            governance_batch_id=batch_id,
        )

        assert second.error_type is None
        assert second.resolved_model_call is not None
        assert second.resolved_model_call.resolved_model_call_id == frozen_call_id
        starts = tuple(
            event
            for event in harness.wiring.event_log.iter()
            if isinstance(event, ModelCallStartEvent)
            and event.resolved_call.purpose is ModelCallPurpose.MEMORY_GOVERNANCE
        )
        assert len(starts) == 1
        assert starts[0].resolved_call.resolved_model_call_id == frozen_call_id

    asyncio.run(scenario())


def test_memory_governance_caller_cancel_detaches_from_owned_preparation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def scenario() -> None:
        harness = _harness(
            tmp_path,
            [
                {"text": _reflection_json()},
                _submit_all_reply,
            ],
        )
        await _produce_reflection_candidate(harness)
        batch_id = "governance:test:cancel-detach"
        entered = threading.Event()
        release = threading.Event()
        archive_type = type(harness.wiring.archive)
        original_put = archive_type.put_text_if_absent_or_confirm_identical

        def blocking_put(self, artifact_id, *args, **kwargs):
            if artifact_id.startswith("governance-batch-input:"):
                entered.set()
                if not release.wait(timeout=10):
                    raise TimeoutError("test governance artifact release timed out")
            return original_put(self, artifact_id, *args, **kwargs)

        monkeypatch.setattr(
            archive_type,
            "put_text_if_absent_or_confirm_identical",
            blocking_put,
        )
        detached = asyncio.create_task(
            harness.engine.run_pending(
                trigger_reason="cancel-detach",
                governance_batch_id=batch_id,
            )
        )
        assert await asyncio.to_thread(entered.wait, 10)
        detached.cancel()
        try:
            await detached
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled governance waiter unexpectedly completed")

        assert harness.engine.execution_owners.pending_count == 1
        joined = asyncio.create_task(
            harness.engine.run_pending(
                trigger_reason="cancel-detach-join",
                governance_batch_id=batch_id,
            )
        )
        release.set()
        result = await asyncio.wait_for(joined, timeout=10)

        assert result.error_type is None
        assert len(result.applied) == 1
        await asyncio.sleep(0)
        assert harness.engine.execution_owners.pending_count == 0
        assert harness.wiring.candidate_pool.list_pending() == []

    asyncio.run(scenario())


def test_memory_governance_engine_merges_one_reflection_batch(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = _harness(
            tmp_path,
            [
                {"text": _reflection_json(two_candidates=True)},
                _merge_all_reply,
            ],
        )
        candidate_ids = await _produce_reflection_candidate(harness)
        assert len(candidate_ids) == 2

        result = await harness.engine.run_pending(
            trigger_reason="test",
            governance_batch_id="governance:test:merge",
        )

        assert result.error_type is None
        assert [decision.kind for decision in result.decisions] == [
            "merge_and_submit"
        ]
        assert harness.wiring.candidate_pool.list_pending() == []
        memories = harness.wiring.graph.find_by_type(
            memory.PREFERENCE,
            graph_id=harness.wiring.graph_id,
        )
        assert len(memories) == 1
        assert memories[0][memory.STATEMENT.name] == (
            "The user prefers concise summaries."
        )
        assert tuple(
            item["candidate"]["candidate_entry_id"]
            for item in _governance_input(harness.transport.contexts[1])["candidates"]
        ) == candidate_ids

    asyncio.run(scenario())


def test_memory_governance_engine_recovers_partial_decision_suffix(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def scenario() -> None:
        harness = _harness(
            tmp_path,
            [
                {"text": _reflection_json(two_candidates=True)},
                _submit_each_reply,
            ],
        )
        candidate_ids = await _produce_reflection_candidate(harness)
        executor_type = type(harness.wiring.memory_governance_executor)
        original_apply = executor_type.apply_decision_async
        call_count = 0

        async def fail_second_apply(self, decision, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("injected second decision failure")
            return await original_apply(self, decision, **kwargs)

        monkeypatch.setattr(executor_type, "apply_decision_async", fail_second_apply)
        batch_id = "governance:test:partial-decision-recovery"
        first = await harness.engine.run_pending(
            trigger_reason="test",
            governance_batch_id=batch_id,
        )

        assert first.error_type == "RuntimeError"
        assert len(first.applied) == 1
        assert harness.engine.retry_required is True
        assert len(harness.wiring.candidate_pool.list_decisions()) == 1
        assert not any(
            isinstance(event, MemoryGovernanceBatchFailedEvent)
            for event in harness.wiring.event_log.iter()
        )
        claims = harness.wiring.memory_governance_claim_repository.claims_for_batch(
            runtime_session_id="runtime:test",
            governance_batch_id=batch_id,
        )
        assert tuple(claim.candidate_entry_id for claim in claims) == candidate_ids
        assert {claim.status for claim in claims} == {
            GovernanceCandidateClaimStatus.PREPARED
        }

        monkeypatch.setattr(executor_type, "apply_decision_async", original_apply)
        second = await harness.engine.run_pending(
            trigger_reason="recovery",
            governance_batch_id=batch_id,
        )

        assert second.error_type is None
        assert len(second.applied) == 2
        assert second.applied[0].diagnostics == ("recovered_existing_decision",)
        assert len(harness.wiring.candidate_pool.list_decisions()) == 2
        completed = tuple(
            event
            for event in harness.wiring.event_log.iter()
            if isinstance(event, MemoryGovernanceBatchCompletedEvent)
        )
        assert len(completed) == 1
        claims = harness.wiring.memory_governance_claim_repository.claims_for_batch(
            runtime_session_id="runtime:test",
            governance_batch_id=batch_id,
        )
        assert {claim.status for claim in claims} == {
            GovernanceCandidateClaimStatus.TERMINAL
        }

    asyncio.run(scenario())


def test_memory_governance_engine_invalid_output_terminalizes_claim(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = _harness(
            tmp_path,
            [
                {"text": _reflection_json()},
                {"text": "not json"},
            ],
        )
        candidate_ids = await _produce_reflection_candidate(harness)

        result = await harness.engine.run_pending(
            trigger_reason="test",
            governance_batch_id="governance:test:invalid-output",
        )

        assert result.error_type == "ValueError"
        assert result.applied == []
        assert harness.wiring.candidate_pool.list_decisions() == []
        assert harness.wiring.graph.find_by_type(
            memory.PREFERENCE,
            graph_id=harness.wiring.graph_id,
        ) == []
        failures = tuple(
            event
            for event in harness.wiring.event_log.iter()
            if isinstance(event, MemoryGovernanceBatchFailedEvent)
        )
        assert len(failures) == 1
        assert failures[0].terminal_reason == "output_invalid"
        claims = harness.wiring.memory_governance_claim_repository.claims_for_batch(
            runtime_session_id="runtime:test",
            governance_batch_id="governance:test:invalid-output",
        )
        assert tuple(claim.candidate_entry_id for claim in claims) == candidate_ids
        assert {claim.status for claim in claims} == {
            GovernanceCandidateClaimStatus.TERMINAL
        }

        contexts_before = len(harness.transport.contexts)
        second = await harness.engine.run_pending(
            trigger_reason="retry",
            governance_batch_id="governance:test:invalid-output",
        )
        assert second.decisions == []
        assert len(harness.transport.contexts) == contexts_before

    asyncio.run(scenario())


def test_memory_governance_engine_rejects_decision_for_unclaimed_id(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = _harness(
            tmp_path,
            [
                {"text": _reflection_json()},
                {"text": _invalid_target_reply()},
            ],
        )
        await _produce_reflection_candidate(harness)

        result = await harness.engine.run_pending(
            trigger_reason="test",
            governance_batch_id="governance:test:bad-target",
        )

        assert result.error_type == "ValueError"
        assert result.applied == []
        assert harness.wiring.candidate_pool.list_decisions() == []
        failure = next(
            event
            for event in harness.wiring.event_log.iter()
            if isinstance(event, MemoryGovernanceBatchFailedEvent)
        )
        assert failure.terminal_reason == "output_invalid"

    asyncio.run(scenario())


def test_memory_governance_result_carries_reported_usage(tmp_path: Path) -> None:
    usage = {
        "input_tokens": 101,
        "cached_input_tokens": 11,
        "output_tokens": 7,
        "reasoning_output_tokens": None,
        "total_tokens": 108,
    }

    def governance_reply(context: LLMContext) -> dict[str, object]:
        reply = _submit_all_reply(context)
        reply["usage"] = usage
        return reply

    async def scenario() -> None:
        harness = _harness(
            tmp_path,
            [
                {"text": _reflection_json()},
                governance_reply,
            ],
        )
        await _produce_reflection_candidate(harness)

        result = await harness.engine.run_pending(trigger_reason="usage-contract")

        assert result.error_type is None
        assert result.resolved_model_call is not None
        assert result.resolved_model_call.purpose is ModelCallPurpose.MEMORY_GOVERNANCE
        assert result.usage_status == "reported"
        assert result.usage == ModelTokenUsageFact.model_validate(usage)
        assert result.estimated_input_tokens is not None
        assert result.reported_model_id == result.resolved_model_call.target.model_id

    asyncio.run(scenario())


def test_memory_governance_empty_pool_does_not_resolve_or_call_model(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = _harness(tmp_path, [])

        result = await harness.engine.run_pending(trigger_reason="test")

        assert result.decisions == []
        assert result.applied == []
        assert result.resolved_model_call is None
        assert harness.transport.contexts == []

    asyncio.run(scenario())


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
                        "contradicted_memory_ids": [
                            "preference:likes-egg-tarts"
                        ],
                        "reason": "The new preference conflicts with the old one.",
                    }
                ],
            }
        )
    )

    assert [decision.kind for decision in output.decisions] == [
        "contradict_and_submit"
    ]
    assert output.decisions[0].contradicted_memory_ids == (
        "preference:likes-egg-tarts",
    )


class _Harness:
    def __init__(
        self,
        *,
        wiring,
        transport: _ScriptedTransport,
        reflection: MemoryReflectionEngine,
        engine: MemoryGovernanceEngine,
    ) -> None:
        self.wiring = wiring
        self.transport = transport
        self.reflection = reflection
        self.engine = engine


def _harness(tmp_path: Path, replies: list[ScriptedReply]) -> _Harness:
    domain = MemoryDomainContext(
        memory_domain_id="u_test",
        workspace_kind="transient",
    )
    wiring = build_in_memory_runtime_wiring(
        tmp_path,
        runtime_session_id="runtime:test",
        memory_domain=domain,
    )
    transport = _ScriptedTransport(replies)
    llm_runtime = _llm_runtime(transport)
    reflection = MemoryReflectionEngine(
        llm_runtime=llm_runtime,
        candidate_pool=wiring.candidate_pool,
        graph=wiring.graph,
        runtime_session=wiring.runtime_session,
        graph_id=wiring.graph_id,
        allowed_scopes=domain.allowed_write_scopes,
        candidate_projection_commit_port=wiring.candidate_projection_commit_port,
    )
    engine = MemoryGovernanceEngine(
        llm_runtime=llm_runtime,
        executor=wiring.memory_governance_executor,
        runtime_session=wiring.runtime_session,
        archive=wiring.archive,
        claim_repository=wiring.memory_governance_claim_repository,
        preparation_repository=wiring.memory_governance_preparation_repository,
        evidence_builder=wiring.memory_governance_evidence_builder,
        preparation_commit_port=wiring.memory_governance_preparation_commit_port,
        candidate_projection_commit_port=wiring.candidate_projection_commit_port,
    )
    return _Harness(
        wiring=wiring,
        transport=transport,
        reflection=reflection,
        engine=engine,
    )


async def _produce_reflection_candidate(harness: _Harness) -> tuple[str, ...]:
    state = LoopState(session_id=harness.wiring.runtime_session.runtime_session_id)
    state.messages.append(
        UserMsg(
            name="user",
            content="Please remember that I prefer concise summaries.",
        )
    )
    if harness.wiring.runtime_session.materialization_account_store.snapshot() is None:
        harness.wiring.runtime_session._adopt_unbootstrapped_in_memory_account_for_test(
            incoming_run_id=state.run_id
        )
    events = await harness.reflection.reflect(
        state=state,
        event_store=harness.wiring.event_log,
        trigger_reasons=["cheap_memory_hint"],
        cheap_hints=[
            MemoryReflectionHint(
                source="cheap_string_match",
                reason="test hint",
                signal="remember",
                excerpt="Please remember that I prefer concise summaries.",
            )
        ],
        safe_point="on_session_end",
    )
    assert events == []
    completed = tuple(
        event
        for event in harness.wiring.event_log.iter()
        if isinstance(event, MemoryReflectionCompletedEvent)
    )
    assert len(completed) == 1
    pending = harness.wiring.candidate_pool.list_pending()
    assert pending
    assert tuple(item.source_event_id for item in pending) == (completed[0].id,) * len(
        pending
    )
    return tuple(sorted(item.entry_id for item in pending))


def _reflection_json(*, two_candidates: bool = False) -> str:
    candidates = [
        {
            "kind": "Preference",
            "statement": "The user prefers concise summaries.",
            "scope": "ctx:user",
            "source_authority": "explicit_user_instruction",
            "verification_status": "user_confirmed",
            "evidence_ids": [],
        }
    ]
    if two_candidates:
        candidates.append(
            {
                "kind": "Preference",
                "statement": "The user prefers brief summaries.",
                "scope": "ctx:user",
                "source_authority": "explicit_user_instruction",
                "verification_status": "user_confirmed",
                "evidence_ids": [],
            }
        )
    return json.dumps(
        {
            "should_reflect": True,
            "reason": "The user explicitly requested a durable preference.",
            "quoted_evidence": [
                "Please remember that I prefer concise summaries."
            ],
            "candidate_kinds": ["Preference"] * len(candidates),
            "summary": "found durable preference evidence",
            "candidates": candidates,
            "skipped_candidates": [],
        }
    )


def _submit_all_reply(context: LLMContext) -> dict[str, object]:
    candidate_ids = _candidate_ids(context)
    return {
        "text": json.dumps(
            {
                "reason": "Durable explicit preference.",
                "decisions": [
                    {
                        "kind": "submit_as_is",
                        "target_entry_id": entry_id,
                        "reason": "The evidence supports this preference.",
                    }
                    for entry_id in candidate_ids
                ],
            }
        )
    }


def _submit_each_reply(context: LLMContext) -> dict[str, object]:
    return {
        "text": json.dumps(
            {
                "reason": "Apply each durable preference independently.",
                "decisions": [
                    {
                        "kind": "submit_as_is",
                        "target_entry_id": candidate_id,
                        "reason": "Durable explicit preference.",
                    }
                    for candidate_id in _candidate_ids(context)
                ],
            }
        )
    }


def _merge_all_reply(context: LLMContext) -> dict[str, object]:
    candidate_ids = _candidate_ids(context)
    return {
        "text": json.dumps(
            {
                "reason": "The candidates express one preference.",
                "decisions": [
                    {
                        "kind": "merge_and_submit",
                        "target_entry_ids": list(candidate_ids),
                        "candidate": {
                            "kind": "Preference",
                            "candidate_id": "candidate:merged-concise-summary",
                            "statement": "The user prefers concise summaries.",
                            "scope": "ctx:user",
                            "source_authority": "explicit_user_instruction",
                            "verification_status": "user_confirmed",
                            "evidence_ids": [],
                        },
                        "reason": "Merge equivalent wording.",
                    }
                ],
            }
        )
    }


def _invalid_target_reply() -> str:
    return json.dumps(
        {
            "reason": "Bad target id.",
            "decisions": [
                {
                    "kind": "submit_as_is",
                    "target_entry_id": "pool:missing",
                    "reason": "This id was not in the frozen input.",
                }
            ],
        }
    )


def _candidate_ids(context: LLMContext) -> tuple[str, ...]:
    payload = _governance_input(context)
    return tuple(
        item["candidate"]["candidate_entry_id"]
        for item in payload["candidates"]
    )


def _governance_input(context: LLMContext) -> dict[str, object]:
    request_messages = tuple(
        message
        for message in context.messages
        if message.role.value == "runtime_request"
    )
    if len(request_messages) != 1:
        raise TypeError("governance test expected one runtime request")
    content = request_messages[0].content[0]
    if not isinstance(content, str):
        raise TypeError("governance test expected one text message")
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise TypeError("governance test input must be an object")
    carrier = parsed.get(RUNTIME_REQUEST_ENVELOPE_KEY)
    if not isinstance(carrier, dict) or carrier.get("request_kind") != (
        "governance_request"
    ):
        raise TypeError("governance test expected a typed runtime request")
    payload = carrier.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("input"), str):
        raise TypeError("governance runtime request payload is malformed")
    model_input = json.loads(payload["input"])
    if not isinstance(model_input, dict):
        raise TypeError("governance model input must be an object")
    return model_input


def _llm_runtime(transport: _ScriptedTransport) -> LLMRuntime:
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api="scripted",
    )
    registry = LLMTransportRegistry()
    registry.register(transport)
    return LLMRuntime(config=config, registry=registry)
