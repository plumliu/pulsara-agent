from __future__ import annotations

import asyncio
from hashlib import sha256

import pytest

from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.event import ContextCompiledEvent, EventContext
from pulsara_agent.llm.config import ModelRole
from pulsara_agent.primitives.context import (
    ContextCandidateSourceSelectionFact,
    ContextCandidateLifecycleKeyFact,
    ContextFactSnapshotFact,
    PreparedContextCandidateEntryFact,
    PreparedContextCandidateSet,
    ContextSectionCandidate,
    context_fingerprint,
)
from pulsara_agent.primitives.context_source import (
    ArtifactContextSourceContentSemanticFact,
    ContextSourceAbsoluteTimingFact,
    InlineContextSourceContentSemanticFact,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.runtime import AgentRuntime, LoopBudget
from pulsara_agent.runtime.context_input.candidate import (
    InMemoryContextLifecycleCache,
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
    current_content = candidate.model_visible_content
    encoded = text.encode("utf-8")
    if isinstance(current_content, InlineContextSourceContentSemanticFact):
        changed_content = build_frozen_fact(
            InlineContextSourceContentSemanticFact,
            schema_version="inline_context_source_content_semantic.v1",
            text=text,
            chars=len(text),
            utf8_bytes=len(encoded),
            media_type=current_content.media_type,
        )
    else:
        assert isinstance(current_content, ArtifactContextSourceContentSemanticFact)
        changed_content = build_frozen_fact(
            ArtifactContextSourceContentSemanticFact,
            schema_version="artifact_context_source_content_semantic.v1",
            content_sha256=f"sha256:{sha256(encoded).hexdigest()}",
            expected_chars=len(text),
            expected_utf8_bytes=len(encoded),
            media_type=current_content.media_type,
            codec_contract_fingerprint=(current_content.codec_contract_fingerprint),
        )
    source_payload = candidate.attribution.semantic.payload
    payload_fields = source_payload.model_dump(
        mode="python", exclude={"semantic_fingerprint"}
    )
    content_field = "prose_content" if "prose_content" in payload_fields else "content"
    payload_fields[content_field] = changed_content
    changed_payload = build_frozen_fact(type(source_payload), **payload_fields)
    semantic_fields = candidate.attribution.semantic.model_dump(
        mode="python", exclude={"semantic_fingerprint"}
    )
    semantic_fields["payload"] = changed_payload
    changed_semantic = build_frozen_fact(
        type(candidate.attribution.semantic), **semantic_fields
    )
    attribution_fields = candidate.attribution.model_dump(
        mode="python", exclude={"fact_fingerprint"}
    )
    attribution_fields["semantic"] = changed_semantic
    changed_attribution = build_frozen_fact(
        type(candidate.attribution), **attribution_fields
    )
    fact_payload = {
        "schema_version": "context_section_candidate.v2",
        "attribution": changed_attribution,
    }
    return ContextSectionCandidate(
        **fact_payload,
        candidate_fingerprint=context_fingerprint(
            "context-section-candidate-fact:v2", fact_payload
        ),
    )


def _candidate_expected_chars(candidate: ContextSectionCandidate) -> int:
    content = candidate.model_visible_content
    if isinstance(content, ArtifactContextSourceContentSemanticFact):
        return content.expected_chars
    assert isinstance(content, InlineContextSourceContentSemanticFact)
    return content.chars


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
    assert by_source["runtime:clock"].lifecycle.status == "freshly_collected"
    assert by_source["runtime:clock"].lifecycle.cache_key is not None


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
    with pytest.raises(ValueError, match="exact snapshot-owned source fact"):
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

    with pytest.raises(ValueError, match="exact snapshot-owned source fact"):
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
        "total_chars": _candidate_expected_chars(entry.candidate),
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
        max_chars=_candidate_expected_chars(entry.candidate),
    )
    cache.put(first_key, entry.candidate)
    cache.put(
        oversize_key,
        _replace_candidate_text(
            entry.candidate,
            "x" * (_candidate_expected_chars(entry.candidate) + 1),
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
    assert system.channel.value == "system"
    assert system.lowering_kind == "system_instruction"
    assert system.attribution.semantic.lowering_intent.role_constraint == "system"


def test_candidate_collector_consumes_snapshot_sources_without_parallel_facade(
    tmp_path, monkeypatch
) -> None:
    prepared = asyncio.run(_prepared_snapshot(tmp_path, monkeypatch))
    for candidate in prepared.invocation.fact.context_source_candidates:
        fields = candidate.attribution.model_dump(mode="python")
        assert "selected_source_ids" not in fields
        assert "omitted_source_count" not in fields
        assert "collection_reason_code" not in fields
    collected = collect_context_candidates(snapshot=prepared.invocation.fact)
    assert tuple(
        entry.candidate.source_instance_id for entry in collected.prepared.entries
    ) == tuple(
        candidate.source_instance_id
        for candidate in sorted(
            prepared.invocation.fact.context_source_candidates,
            key=lambda item: (
                not item.required,
                item.priority,
                item.source_id.value,
                item.source_instance_id,
                item.attribution.semantic.candidate_key,
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


def test_required_source_above_legacy_64k_uses_resolved_physical_policy(
    tmp_path, monkeypatch
) -> None:
    import pulsara_agent.runtime.agent as agent_module

    system_prompt = "system source above the removed fixed cap\n" + ("x" * 70_000)
    transport = ScriptedTransport([{"text": "done"}])
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
        llm_runtime=make_llm_runtime(transport),
        system_prompt=system_prompt,
    )

    result = asyncio.run(run_agent_task(agent, "verify resolved source policy"))

    assert result.status.value == "finished"
    assert transport.contexts[0].system_prompt == system_prompt
    system_candidate = next(
        candidate
        for candidate in captured[0].snapshot_build_input.context_source_candidates
        if candidate.source_instance_id == "system:prompt"
    )
    assert isinstance(
        system_candidate.model_visible_content,
        ArtifactContextSourceContentSemanticFact,
    )
    assert system_prompt not in str(system_candidate.model_dump(mode="python"))


def test_legacy_candidate_collection_input_is_physically_removed() -> None:
    import pulsara_agent.runtime.context_input.candidate as candidate_module

    assert not hasattr(candidate_module, "ContextCandidateCollectionInput")


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
        "subagent_graph_semantic_source": (snapshot.subagent_graph_semantic_source),
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


def test_candidate_source_attribution_must_match_snapshot_authority(
    tmp_path, monkeypatch
) -> None:
    prepared = asyncio.run(_prepared_snapshot(tmp_path, monkeypatch))
    original = next(
        entry.candidate
        for entry in prepared.prepared_candidates.entries
        if entry.candidate.source_instance_id == "system:prompt"
    )
    forged_timing = build_frozen_fact(
        ContextSourceAbsoluteTimingFact,
        schema_version="context_source_absolute_timing.v1",
        observed_at_utc="2000-01-01T00:00:00.000000Z",
        source_started_at_utc=None,
        source_ended_at_utc=None,
        source_sequence_ranges=(),
        freshness_kind="historical_replay",
        clock_source="host_clock",
        timing_contract_fingerprint=context_fingerprint(
            "test-context-source-timing-contract:v1", "forged"
        ),
    )
    attribution_fields = original.attribution.model_dump(
        mode="python", exclude={"fact_fingerprint"}
    )
    attribution_fields["source_absolute_timing"] = forged_timing
    forged_attribution = build_frozen_fact(
        type(original.attribution), **attribution_fields
    )
    fact_payload = {
        "schema_version": "context_section_candidate.v2",
        "attribution": forged_attribution,
    }
    forged = ContextSectionCandidate(
        **fact_payload,
        candidate_fingerprint=context_fingerprint(
            "context-section-candidate-fact:v2", fact_payload
        ),
    )

    with pytest.raises(ValueError, match="exact snapshot-owned source fact"):
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
    assert (
        first_system.candidate.attribution.semantic
        == second_system.candidate.attribution.semantic
    )
    assert first_system.candidate.attribution != second_system.candidate.attribution
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
