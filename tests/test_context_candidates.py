from __future__ import annotations

import asyncio

import pytest

from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.event import ContextCompiledEvent, EventContext
from pulsara_agent.llm.config import ModelRole
from pulsara_agent.primitives.context import (
    ContextCandidateSourceSelectionFact,
    ContextCandidateLifecycleKeyFact,
    ContextFactSnapshotFact,
    ContextInlineTextFact,
    ContextSourceTimingFact,
    PreparedContextCandidateEntryFact,
    PreparedContextCandidateSet,
    ContextSectionCandidate,
    context_fingerprint,
)
from pulsara_agent.runtime import AgentRuntime, LoopBudget
from pulsara_agent.runtime.context_input.candidate import (
    InMemoryContextLifecycleCache,
    ContextCandidateCollectionInput,
    build_context_candidate_source_selections,
    collect_context_candidates,
    prepare_context_candidates,
)
from pulsara_agent.runtime.context_input.compiler import compile_context_from_facts
from pulsara_agent.runtime.context_input.policy import resolve_context_compile_policy
from pulsara_agent.runtime.context_input.render import (
    prepare_tool_result_render_input,
    render_prepared_tool_result_units,
)
from pulsara_agent.runtime.context_input.compiler import _apply_section_budget
from pulsara_agent.runtime.context_engine.types import AllocatedContextSection
from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV1
from tests.support.runtime_session import in_memory_runtime_session
from tests.conftest import open_test_root_rollout_run
from tests.test_agent_runtime_loop import (
    ScriptedTransport,
    make_llm_runtime,
    run_agent_task,
)


async def _prepared_snapshot(tmp_path, monkeypatch):
    import pulsara_agent.runtime.agent as agent_module

    captured = []
    original = agent_module.prepare_live_context_snapshot

    async def capture(**kwargs):
        prepared = await original(**kwargs)
        captured.append(prepared)
        return prepared

    monkeypatch.setattr(agent_module, "prepare_live_context_snapshot", capture)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(ScriptedTransport([{"text": "done"}])),
    )
    await run_agent_task(agent, "candidate facts")
    return captured[0]


async def _compiled_snapshot(tmp_path, monkeypatch) -> ContextFactSnapshotFact:
    import pulsara_agent.runtime.agent as agent_module

    captured: list[ContextFactSnapshotFact] = []
    original = agent_module.build_context_snapshot

    def capture(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        captured.append(snapshot)
        return snapshot

    monkeypatch.setattr(agent_module, "build_context_snapshot", capture)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(ScriptedTransport([{"text": "done"}])),
    )
    await run_agent_task(agent, "candidate facts")
    return captured[-1]


class _CandidateCache:
    def __init__(self) -> None:
        self.values: dict[
            ContextCandidateLifecycleKeyFact, ContextSectionCandidate
        ] = {}

    def get(self, key: ContextCandidateLifecycleKeyFact):
        return self.values.get(key)

    def put(
        self,
        key: ContextCandidateLifecycleKeyFact,
        candidate: ContextSectionCandidate,
    ) -> None:
        self.values[key] = candidate


def _replace_candidate_text(
    candidate: ContextSectionCandidate, text: str
) -> ContextSectionCandidate:
    inline = ContextInlineTextFact(
        text=text,
        chars=len(text),
        content_fingerprint=context_fingerprint("context-inline-text:v1", text),
    )
    payload = candidate.model_dump(
        mode="python",
        exclude={"semantic_fingerprint", "candidate_fingerprint"},
    )
    payload["payload"] = inline
    semantic_payload = {
        key: value for key, value in payload.items() if key != "candidate_id"
    }
    semantic = context_fingerprint(
        "context-section-candidate-semantic:v1", semantic_payload
    )
    fact_payload = {**payload, "semantic_fingerprint": semantic}
    return ContextSectionCandidate(
        **fact_payload,
        candidate_fingerprint=context_fingerprint(
            "context-section-candidate-fact:v1", fact_payload
        ),
    )


def test_context_candidates_freeze_cacheable_and_ephemeral_lifecycle(
    tmp_path, monkeypatch
) -> None:
    prepared = asyncio.run(_prepared_snapshot(tmp_path, monkeypatch))
    by_source = {
        entry.candidate.source_instance_id: entry
        for entry in prepared.prepared_candidates.entries
    }
    assert by_source["system:prompt"].lifecycle.status == "freshly_collected"
    assert by_source["system:prompt"].lifecycle.cache_key is not None
    assert by_source["runtime_context"].lifecycle.status == "not_cacheable"
    assert by_source["runtime_context"].lifecycle.cache_key is None


def test_candidate_cache_reuse_requires_snapshot_authorized_payload(
    tmp_path, monkeypatch
) -> None:
    prepared = asyncio.run(_prepared_snapshot(tmp_path, monkeypatch))
    snapshot = prepared.invocation.fact
    system_entry = next(
        entry
        for entry in prepared.prepared_candidates.entries
        if entry.candidate.source_instance_id == "system:prompt"
    )
    assert system_entry.lifecycle.cache_key is not None
    cache = _CandidateCache()
    cache.put(system_entry.lifecycle.cache_key, system_entry.candidate)
    reused, writes, operational = prepare_context_candidates(
        snapshot=snapshot,
        candidates=(system_entry.candidate,),
        collection_decisions=(),
        cache=cache,
    )
    assert reused.entries[0].lifecycle.status == "reused"
    assert writes == ()
    assert operational == ()

    changed = _replace_candidate_text(system_entry.candidate, "changed projection")
    with pytest.raises(ValueError, match="payload differs from snapshot authority"):
        prepare_context_candidates(
            snapshot=snapshot,
            candidates=(changed,),
            collection_decisions=(),
            cache=cache,
        )


def test_compiler_rejects_forged_candidate_even_with_valid_candidate_fingerprint(
    tmp_path,
    monkeypatch,
) -> None:
    prepared = asyncio.run(_prepared_snapshot(tmp_path, monkeypatch))
    original_set = prepared.prepared_candidates
    original_entry = next(
        entry
        for entry in original_set.entries
        if entry.candidate.source_instance_id == "system:prompt"
    )
    forged = _replace_candidate_text(original_entry.candidate, "forged system")
    forged_entries = tuple(
        PreparedContextCandidateEntryFact(
            candidate=forged,
            lifecycle=entry.lifecycle,
        )
        if entry is original_entry
        else entry
        for entry in original_set.entries
    )
    set_payload = original_set.model_dump(
        mode="python", exclude={"candidate_set_fingerprint"}
    )
    set_payload["entries"] = forged_entries
    forged_set = PreparedContextCandidateSet(
        **set_payload,
        candidate_set_fingerprint=context_fingerprint(
            "prepared-context-candidate-set:v1", set_payload
        ),
    )
    rendered = render_prepared_tool_result_units(
        prepared=prepared.prepared_tool_results,
        transcript=prepared.normalized_transcript.transcript,
        token_estimator=prepared.invocation.resolved_call.target.token_estimator,
    )

    with pytest.raises(ValueError, match="payload differs from snapshot authority"):
        compile_context_from_facts(
            facts=prepared.invocation,
            transcript=prepared.normalized_transcript.transcript,
            rendered_tool_results=rendered,
            prepared_rollups=(),
            section_candidates=forged_set,
        )


def test_candidate_lifecycle_cache_is_bounded_lru_and_records_eviction(
    tmp_path,
    monkeypatch,
) -> None:
    prepared = asyncio.run(_prepared_snapshot(tmp_path, monkeypatch))
    entry = next(
        item
        for item in prepared.prepared_candidates.entries
        if item.candidate.source_instance_id == "system:prompt"
    )
    assert entry.lifecycle.cache_key is not None
    first_key = entry.lifecycle.cache_key
    second_key = first_key.model_copy(update={"scope_id": "runtime:second"})
    cache = InMemoryContextLifecycleCache(max_entries=1, max_chars=100_000)

    cache.put(first_key, entry.candidate)
    cache.put(second_key, entry.candidate)

    assert cache.get(first_key) is None
    assert cache.get(second_key) == entry.candidate
    assert cache.stats() == {
        "entry_count": 1,
        "total_chars": entry.candidate.payload.chars,
        "max_entries": 1,
        "max_chars": 100_000,
        "eviction_count": 1,
        "skipped_oversize_entries": 0,
    }


def test_candidate_lifecycle_cache_skips_oversize_without_evicting_existing(
    tmp_path,
    monkeypatch,
) -> None:
    prepared = asyncio.run(_prepared_snapshot(tmp_path, monkeypatch))
    entry = next(
        item
        for item in prepared.prepared_candidates.entries
        if item.candidate.source_instance_id == "system:prompt"
    )
    assert entry.lifecycle.cache_key is not None
    first_key = entry.lifecycle.cache_key
    oversize_key = first_key.model_copy(update={"scope_id": "runtime:oversize"})
    cache = InMemoryContextLifecycleCache(
        max_entries=2,
        max_chars=entry.candidate.payload.chars,
    )
    cache.put(first_key, entry.candidate)
    cache.put(
        oversize_key,
        _replace_candidate_text(
            entry.candidate,
            entry.candidate.payload.text + " oversized",
        ),
    )

    assert cache.get(first_key) == entry.candidate
    assert cache.get(oversize_key) is None
    assert cache.stats()["entry_count"] == 1
    assert cache.stats()["eviction_count"] == 0
    assert cache.stats()["skipped_oversize_entries"] == 1


def test_candidate_cache_read_failure_is_not_durable_semantic_input(
    tmp_path,
    monkeypatch,
) -> None:
    class FailingCache:
        def get(self, _key):
            raise RuntimeError("cache unavailable")

    prepared = asyncio.run(_prepared_snapshot(tmp_path, monkeypatch))
    snapshot = prepared.invocation.fact
    system = next(
        entry.candidate
        for entry in prepared.prepared_candidates.entries
        if entry.candidate.source_instance_id == "system:prompt"
    )
    miss, miss_writes, miss_operational = prepare_context_candidates(
        snapshot=snapshot,
        candidates=(system,),
        collection_decisions=(),
        cache=_CandidateCache(),
    )
    failed, failed_writes, failed_operational = prepare_context_candidates(
        snapshot=snapshot,
        candidates=(system,),
        collection_decisions=(),
        cache=FailingCache(),
    )

    assert miss == failed
    assert miss.candidate_set_fingerprint == failed.candidate_set_fingerprint
    assert miss_writes == failed_writes
    assert miss_operational == ()
    assert len(failed_operational) == 1
    assert failed_operational[0].operation == "read"
    assert isinstance(failed_operational[0].error, RuntimeError)


def test_candidate_channel_lowering_matrix_is_schema_enforced(
    tmp_path,
    monkeypatch,
) -> None:
    prepared = asyncio.run(_prepared_snapshot(tmp_path, monkeypatch))
    system = next(
        entry.candidate
        for entry in prepared.prepared_candidates.entries
        if entry.candidate.source_instance_id == "system:prompt"
    )
    payload = system.model_dump(
        mode="python",
        exclude={"semantic_fingerprint", "candidate_fingerprint"},
    )
    payload["lowering_kind"] = "leading_user_context"
    semantic_payload = {
        key: value for key, value in payload.items() if key != "candidate_id"
    }
    semantic = context_fingerprint(
        "context-section-candidate-semantic:v1", semantic_payload
    )
    fact_payload = {**payload, "semantic_fingerprint": semantic}
    with pytest.raises(ValueError, match="channel/lowering matrix"):
        ContextSectionCandidate(
            **fact_payload,
            candidate_fingerprint=context_fingerprint(
                "context-section-candidate-fact:v1", fact_payload
            ),
        )


def test_candidate_collector_consumes_snapshot_authority_without_parallel_sources(
    tmp_path, monkeypatch
) -> None:
    prepared = asyncio.run(_prepared_snapshot(tmp_path, monkeypatch))
    for authority in prepared.invocation.fact.candidate_authorities:
        fields = authority.model_dump(mode="python")
        assert "selected_source_ids" not in fields
        assert "omitted_source_count" not in fields
        assert "collection_reason_code" not in fields
    collected = collect_context_candidates(snapshot=prepared.invocation.fact)
    assert tuple(
        entry.candidate.source_instance_id for entry in collected.prepared.entries
    ) == tuple(
        authority.source_instance_id
        for authority in sorted(
            prepared.invocation.fact.candidate_authorities,
            key=lambda item: (
                not item.required,
                item.priority,
                item.source_instance_id,
            ),
        )
    )
    selection = prepared.invocation.fact.candidate_source_selections[0]
    assert selection.source_instance_id == "subagent:results"
    assert selection.eligible_source_count == 0
    assert selection.reason_code == "no_eligible_sources"
    decision = next(
        item
        for item in collected.prepared.collection_decisions
        if item.source_kind == "subagent_results"
    )
    assert decision.selected_source_ids == ()
    assert decision.omitted_source_count == 0
    assert decision.reason_code == "no_eligible_sources"


def test_candidate_collection_input_rejects_process_local_subagent_selection() -> None:
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        ContextCandidateCollectionInput(
            system_prompt="system",
            subagent_selected_result_ids=("result:1",),  # type: ignore[call-arg]
        )


def test_snapshot_rejects_subagent_selection_above_frozen_policy(
    tmp_path, monkeypatch
) -> None:
    snapshot = asyncio.run(_compiled_snapshot(tmp_path, monkeypatch))
    selected_ids = tuple(f"result:{index}" for index in range(9))
    selection_payload = {
        "source_instance_id": "subagent:results",
        "eligible_source_count": len(selected_ids),
        "selected_source_ids": selected_ids,
        "omitted_source_count": 0,
        "reason_code": "selected_all",
        "policy_fingerprint": (
            snapshot.compile_policy.candidate_collection.policy_fingerprint
        ),
        "subagent_graph_semantic_source": (
            snapshot.subagent_graph_semantic_source
        ),
    }
    forged_selection = ContextCandidateSourceSelectionFact(
        **selection_payload,
        selection_fingerprint=context_fingerprint(
            "context-candidate-source-selection:v1", selection_payload
        ),
    )
    snapshot_payload = snapshot.model_dump(
        mode="python",
        exclude={"snapshot_semantic_fingerprint", "snapshot_fact_fingerprint"},
    )
    snapshot_payload["candidate_source_selections"] = (forged_selection,)

    with pytest.raises(ValueError, match="selection exceeds compile policy"):
        ContextFactSnapshotFact(
            **snapshot_payload,
            snapshot_semantic_fingerprint="sha256:" + "0" * 64,
            snapshot_fact_fingerprint="sha256:" + "0" * 64,
        )


def test_new_child_completion_between_reads_cannot_drift_selection_high_water(
    tmp_path,
) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(ScriptedTransport([{"text": "unused"}])),
    )
    assert agent.subagent_runtime is not None
    event_context = EventContext(
        run_id="run:frozen-selection",
        turn_id="turn:frozen-selection",
        reply_id="reply:frozen-selection",
    )

    async def scenario():
        from pulsara_agent.runtime.long_horizon.checkpoint import (
            restore_subagent_graph_from_checkpoint,
        )

        # This fixture exercises only canonical graph selection high-waters.
        # Fake child completion has no child-native rollout ledger, so keep the
        # production rollout settlement hooks outside this graph-only test.
        agent.subagent_runtime.bind_rollout_admission(None)
        agent.subagent_runtime.bind_rollout_terminal_augmenter(None)
        open_test_root_rollout_run(
            runtime_session,
            event_context=event_context,
            model_target=agent.llm_runtime.resolve_target(role=ModelRole.PRO).fact,
        )
        run = await agent.subagent_runtime.spawn_fake(
            task="complete after slice",
            event_context=event_context,
        )
        service = runtime_session.subagent_graph_checkpoint_service
        old_high_water = runtime_session.event_log.next_sequence() - 1
        old_read = await service.restore_for_selection(
            requested_through_sequence=old_high_water
        )
        old_graph, old_semantic, _old_acceleration = (
            restore_subagent_graph_from_checkpoint(
                snapshot=old_read,
                reducer_binding=service.reducer_binding,
            )
        )
        await agent.subagent_runtime.complete_fake(
            run.subagent_run_id,
            summary="completed after frozen high-water",
            event_context=event_context,
        )
        new_high_water = runtime_session.event_log.next_sequence() - 1
        new_read = await service.restore_for_selection(
            requested_through_sequence=new_high_water
        )
        new_graph, new_semantic, _new_acceleration = (
            restore_subagent_graph_from_checkpoint(
                snapshot=new_read,
                reducer_binding=service.reducer_binding,
            )
        )
        return old_graph, old_semantic, new_graph, new_semantic

    old_graph, old_semantic, new_graph, new_semantic = asyncio.run(scenario())
    policy = resolve_context_compile_policy(
        LoopBudget(max_subagent_results_per_parent_compile=0)
    ).candidate_collection
    old_selection = build_context_candidate_source_selections(
        subagent_graph=old_graph,
        semantic_source=old_semantic,
        policy=policy,
    )[0]
    new_selection = build_context_candidate_source_selections(
        subagent_graph=new_graph,
        semantic_source=new_semantic,
        policy=policy,
    )[0]

    assert old_selection.eligible_source_count == 0
    assert old_selection.reason_code == "no_eligible_sources"
    assert old_selection.subagent_graph_semantic_source == old_semantic
    assert new_selection.eligible_source_count == 1
    assert new_selection.selected_source_ids == ()
    assert new_selection.omitted_source_count == 1
    assert new_selection.reason_code == "policy_limit"
    assert new_selection.subagent_graph_semantic_source == new_semantic


def test_candidate_source_timing_must_match_snapshot_authority(
    tmp_path, monkeypatch
) -> None:
    prepared = asyncio.run(_prepared_snapshot(tmp_path, monkeypatch))
    original = next(
        entry.candidate
        for entry in prepared.prepared_candidates.entries
        if entry.candidate.source_instance_id == "system:prompt"
    )
    timing_payload = {
        "observed_at_utc": "2000-01-01T00:00:00.000000Z",
        "source_started_at_utc": None,
        "source_ended_at_utc": None,
        "source_sequence_start": None,
        "source_sequence_end": None,
        "freshness": "subagent_result",
        "clock_source": "host_clock",
    }
    forged_timing = ContextSourceTimingFact(
        **timing_payload,
        timing_fingerprint=context_fingerprint(
            "context-source-timing:v1", timing_payload
        ),
    )
    candidate_payload = original.model_dump(
        mode="python", exclude={"semantic_fingerprint", "candidate_fingerprint"}
    )
    candidate_payload["source_timing"] = forged_timing
    semantic_payload = {
        key: value for key, value in candidate_payload.items() if key != "candidate_id"
    }
    semantic = context_fingerprint(
        "context-section-candidate-semantic:v1", semantic_payload
    )
    fact_payload = {**candidate_payload, "semantic_fingerprint": semantic}
    forged = ContextSectionCandidate(
        **fact_payload,
        candidate_fingerprint=context_fingerprint(
            "context-section-candidate-fact:v1", fact_payload
        ),
    )

    with pytest.raises(ValueError, match="attribution differs from snapshot authority"):
        prepare_context_candidates(
            snapshot=prepared.invocation.fact,
            candidates=(forged,),
            collection_decisions=(),
            cache=None,
        )


def test_candidate_collection_and_lowering_order_use_priority_not_field_order(
    tmp_path,
    monkeypatch,
) -> None:
    prepared = asyncio.run(_prepared_snapshot(tmp_path, monkeypatch))
    priorities = tuple(
        entry.candidate.priority for entry in prepared.prepared_candidates.entries
    )
    assert priorities == tuple(sorted(priorities))
    assert prepared.prepared_candidates.entries[0].candidate.source_instance_id == (
        "system:prompt"
    )


def test_budget_omits_low_priority_optional_system_before_required_fact() -> None:
    sections = (
        AllocatedContextSection(
            id="required",
            source_id="required",
            channel="system",
            priority=100,
            stability="run",
            budget_class="must_keep",
            text="required",
            estimated_tokens=10,
        ),
        AllocatedContextSection(
            id="important",
            source_id="important",
            channel="leading_user",
            priority=10,
            stability="run",
            budget_class="important",
            text="important",
            estimated_tokens=10,
        ),
        AllocatedContextSection(
            id="optional-system",
            source_id="optional-system",
            channel="system",
            priority=60,
            stability="run",
            budget_class="important",
            text="optional",
            estimated_tokens=10,
        ),
    )
    allocated, _ = _apply_section_budget(
        sections,
        (),
        input_budget_tokens=20,
        tools_estimated_tokens=0,
        estimator=PulsaraHeuristicTokenEstimatorV1(),
    )
    by_id = {section.id: section for section in allocated}
    assert by_id["required"].included is True
    assert by_id["important"].included is True
    assert by_id["optional-system"].included is False


def test_model_followup_reuses_session_owned_candidate_cache(
    tmp_path, monkeypatch
) -> None:
    import pulsara_agent.runtime.agent as agent_module

    captured = []
    original = agent_module.prepare_live_context_snapshot

    async def capture(**kwargs):
        prepared = await original(**kwargs)
        captured.append(prepared)
        return prepared

    monkeypatch.setattr(agent_module, "prepare_live_context_snapshot", capture)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(
            ScriptedTransport(
                [
                    {
                        "tool_calls": [
                            {
                                "id": "call:unknown",
                                "name": "unknown_contract_tool",
                                "arguments": "{}",
                            }
                        ]
                    },
                    {"text": "done"},
                ]
            )
        ),
    )
    asyncio.run(run_agent_task(agent, "exercise lifecycle cache"))
    assert len(captured) == 2
    first_system = next(
        entry
        for entry in captured[0].prepared_candidates.entries
        if entry.candidate.source_instance_id == "system:prompt"
    )
    second_system = next(
        entry
        for entry in captured[1].prepared_candidates.entries
        if entry.candidate.source_instance_id == "system:prompt"
    )
    assert first_system.lifecycle.status == "freshly_collected"
    assert second_system.lifecycle.status == "reused"
    assert first_system.candidate == second_system.candidate
    compiled = [
        event
        for event in agent.runtime_session.event_log.iter()
        if isinstance(event, ContextCompiledEvent) and event.status == "compiled"
    ]
    assert any(
        item.get("status") == "freshly_collected"
        for item in compiled[0].lifecycle_decisions
    )
    assert any(
        item.get("status") == "reused" for item in compiled[1].lifecycle_decisions
    )
    second = captured[1]
    replay_prepared = prepare_tool_result_render_input(
        units=second.normalized_transcript.tool_result_units,
        transcript=second.normalized_transcript.transcript,
        policy_basis=second.invocation.fact.compile_policy.tool_result_basis,
        cache=agent.runtime_session.tool_result_render_cache,
    )
    replay_rendered = render_prepared_tool_result_units(
        prepared=replay_prepared,
        transcript=second.normalized_transcript.transcript,
        token_estimator=agent.resolve_run_model_target().token_estimator,
    )
    assert replay_rendered.operational_facts
    assert all(fact.cache_status == "hit" for fact in replay_rendered.operational_facts)
    assert replay_rendered.cache_write_candidates == ()
