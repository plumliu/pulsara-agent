from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.event import (
    ContextCompiledEvent,
    RunStartEvent,
    ExistingGenerationPreparationAbandonedEvent,
    ProviderInputAppendCommittedEvent,
    ProviderInputGenerationClosedEvent,
)
from pulsara_agent.llm import LLMRuntime
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.context_source import (
    ContextSourceId,
    ContextSourceInputAuthorityFact,
    LedgerAuthorityHorizonFact,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.runtime import AgentRuntime, LoopStatus
from pulsara_agent.runtime.hooks import NoopMemoryHooks
from pulsara_agent.runtime.context_input.sources.builder import (
    default_context_source_registry,
)
from pulsara_agent.runtime.context_input.sources.input import (
    CONTEXT_SOURCE_INPUT_TYPES,
    context_source_input_dependency_fingerprint,
)
from pulsara_agent.runtime.context_input.sources.render import (
    render_context_source_candidate,
)
from pulsara_agent.runtime.provider_input.materialization import (
    append_carrier,
    freeze_message_unit,
    hydrate_carrier,
    validate_dispatch_context_against_plan,
)
from pulsara_agent.runtime.provider_input.causal import (
    build_default_resolved_causal_physical_policy,
)
from pulsara_agent.runtime.provider_input.planner import (
    _validate_append_artifact_physical_bound,
)
from pulsara_agent.runtime.provider_input.vector import (
    APPEND_MAX_UNITS,
    MAX_CHANGED_LEAVES,
    MAX_CHANGED_NODES,
    append_provider_input_vector,
    prepare_provider_input_vector,
)
from pulsara_agent.inspector.service import (
    _provider_input_generation_projection,
    _referenced_provider_input_generation_ids,
)
from tests.support import (
    run_agent_task,
    test_llm_config,
    test_model_limits,
)
from tests.support.runtime_session import in_memory_runtime_session
from tests.test_agent_runtime_loop import ScriptedTransport, make_llm_runtime


class _OversizedOptionalMemoryInstruction(NoopMemoryHooks):
    def memory_context_prompt(self) -> str:
        return "M" * (4 * 1024 * 1024 + 1)


class _SmallMemoryInstruction(NoopMemoryHooks):
    def memory_context_prompt(self) -> str:
        return "MEMORY_SCOPE_FROM_OWNED_SOURCE"


class _BudgetPressureMemoryInstruction(NoopMemoryHooks):
    def memory_context_prompt(self) -> str:
        return "PROVIDER_SOURCE_MUST_BE_OMITTED_" * 800


async def _capture_prepared_snapshot(tmp_path, monkeypatch):
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
    result = await run_agent_task(agent, "capture provider source authority")
    assert result.status is LoopStatus.FINISHED
    return captured[0]


def _authorized_input_from_candidate(candidate, *, physical_policy_fingerprint):
    registry = default_context_source_registry()
    semantic = candidate.attribution.semantic
    attribution = candidate.attribution
    binding = registry.resolve(source_id=semantic.source_id)
    dependency = context_source_input_dependency_fingerprint(
        source_instance_id=semantic.source_instance_id,
        candidate_key=semantic.candidate_key,
        source_revision=semantic.source_revision,
        payload=semantic.payload,
        lifecycle=semantic.lifecycle,
        priority=semantic.priority,
        required=semantic.required,
        lowering_intent=semantic.lowering_intent,
        source_event_refs=attribution.source_event_refs,
        source_artifact_refs=attribution.source_artifact_refs,
        source_absolute_timing=attribution.source_absolute_timing,
        source_contract_fingerprint=binding.contract.contract_fingerprint,
    )
    authority = build_frozen_fact(
        ContextSourceInputAuthorityFact,
        schema_version="context_source_input_authority.v1",
        source_id=semantic.source_id,
        source_contract_id=semantic.source_id.value,
        source_contract_version=binding.contract.source_version,
        source_contract_fingerprint=binding.contract.contract_fingerprint,
        authority_horizons=attribution.authority_horizons,
        physical_input_policy_fingerprint=physical_policy_fingerprint,
        input_dependency_fingerprint=dependency,
    )
    input_type = CONTEXT_SOURCE_INPUT_TYPES[semantic.source_id]
    return registry, input_type(
        authority=authority,
        source_instance_id=semantic.source_instance_id,
        candidate_key=semantic.candidate_key,
        source_revision=semantic.source_revision,
        payload=semantic.payload,
        lifecycle=semantic.lifecycle,
        priority=semantic.priority,
        required=semantic.required,
        lowering_intent=semantic.lowering_intent,
        source_event_refs=attribution.source_event_refs,
        source_artifact_refs=attribution.source_artifact_refs,
        source_absolute_timing=attribution.source_absolute_timing,
    )


def _horizon() -> LedgerAuthorityHorizonFact:
    return build_frozen_fact(
        LedgerAuthorityHorizonFact,
        schema_version="ledger_authority_horizon.v1",
        runtime_session_id="runtime:provider-vector",
        through_sequence=1,
        ledger_event_count_through=1,
        ledger_continuity_accumulator_through=context_fingerprint(
            "test-ledger-prefix:v1", "one"
        ),
    )


def _message_unit(index: int):
    return freeze_message_unit(
        LLMMessage.user(f"message:{index}"),
        unit_kind="transcript_message",
        owner_semantic_fingerprint=context_fingerprint(
            "test-provider-unit-owner:v1", index
        ),
        authority_horizons=(_horizon(),),
        estimated_tokens=1,
    )


def _distinct_ordered_projections(compiled):
    by_identity = {}
    for item in compiled:
        prepared = item.prepared_ordered_transcript_projection
        if prepared is None:
            continue
        by_identity.setdefault(
            prepared.identity.identity_fingerprint,
            prepared.projection,
        )
    return tuple(by_identity.values())


def test_manifest_failure_retires_pre_start_provider_preparation(
    tmp_path, monkeypatch
) -> None:
    import pulsara_agent.runtime.agent as agent_module

    def reject_manifest(*args, **kwargs):
        raise RuntimeError("synthetic manifest construction failure")

    monkeypatch.setattr(agent_module, "build_context_input_manifest", reject_manifest)
    runtime_session = in_memory_runtime_session(tmp_path)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(ScriptedTransport([{"text": "unused"}])),
    )

    result = asyncio.run(run_agent_task(agent, "fail after provider preparation"))

    assert result.status is LoopStatus.FAILED
    assert (
        runtime_session.provider_input_generation_store.active_preparation_snapshots()
        == ()
    )
    assert (
        runtime_session.provider_input_generation_coordinator.owned_preparation_count
        == 0
    )


def test_registry_rejection_transfers_and_terminalizes_preparation_owner(
    tmp_path, monkeypatch
) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)

    def reject_install(**kwargs):
        raise RuntimeError("synthetic registry rejection")

    monkeypatch.setattr(
        runtime_session.model_stream_execution_registry,
        "install_and_start",
        reject_install,
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(ScriptedTransport([{"text": "unused"}])),
    )

    result = asyncio.run(run_agent_task(agent, "reject before ModelStart"))

    assert result.status is LoopStatus.FAILED
    assert (
        runtime_session.provider_input_generation_store.active_preparation_snapshots()
        == ()
    )
    assert (
        runtime_session.provider_input_generation_coordinator.owned_preparation_count
        == 0
    )
    assert any(
        isinstance(event, ExistingGenerationPreparationAbandonedEvent)
        or event.type.value == "PROVIDER_INPUT_SCOPED_PREPARATION_ABANDONED"
        for event in runtime_session.event_log.iter()
    )


def test_provider_input_carrier_deep_copies_tool_schema_at_dispatch(
    tmp_path, monkeypatch
) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)
    coordinator = runtime_session.provider_input_generation_coordinator
    captured = []
    original = coordinator.prepare_compiled_call

    async def capture(**kwargs):
        bundle = await original(**kwargs)
        captured.append(bundle)
        return bundle

    monkeypatch.setattr(coordinator, "prepare_compiled_call", capture)
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(transport),
    )

    result = asyncio.run(run_agent_task(agent, "freeze provider tools"))

    assert result.status is LoopStatus.FINISHED
    bundle = captured[0]
    first = bundle.carrier.to_llm_context(transport.contexts[0])
    second = bundle.carrier.to_llm_context(transport.contexts[0])
    assert first.tools and first.tools[0] is not second.tools[0]
    first.tools[0].parameters["pulsara_mutation_probe"] = {"type": "string"}
    assert "pulsara_mutation_probe" not in second.tools[0].parameters
    with pytest.raises(ValueError, match="dispatch tool catalog drifted"):
        validate_dispatch_context_against_plan(
            context=first,
            plan=bundle.canonical_plan,
        )


def test_context_source_registry_rejects_self_certified_policy_and_payload(
    tmp_path, monkeypatch
) -> None:
    prepared = asyncio.run(_capture_prepared_snapshot(tmp_path, monkeypatch))
    candidates = prepared.invocation.fact.context_source_candidates
    system = next(
        item for item in candidates if item.source_id is ContextSourceId.SYSTEM
    )
    runtime_environment = next(
        item
        for item in candidates
        if item.source_id is ContextSourceId.RUNTIME_ENVIRONMENT
    )
    registry, source_input = _authorized_input_from_candidate(
        system,
        physical_policy_fingerprint=(
            prepared.invocation.fact.context_source_physical_input_policy.policy_fingerprint
        ),
    )
    assert registry.collect((source_input,)) == (system,)

    with pytest.raises(ValueError, match="priority is unauthorized"):
        registry.collect((replace(source_input, priority=-999),))
    with pytest.raises(ValueError, match="payload type is unauthorized"):
        registry.collect(
            (
                replace(
                    source_input,
                    payload=runtime_environment.attribution.semantic.payload,
                ),
            )
        )


def test_unselected_optional_artifact_is_not_eagerly_hydrated(
    tmp_path, monkeypatch
) -> None:
    import pulsara_agent.runtime.agent as agent_module

    runtime_session = in_memory_runtime_session(tmp_path)
    read_artifact_ids: list[str] = []
    archive_type = type(runtime_session.archive)
    original_get_text = archive_type.get_text

    def record_get_text(self, artifact_id, *args, **kwargs):
        if self is runtime_session.archive:
            read_artifact_ids.append(artifact_id)
        return original_get_text(self, artifact_id, *args, **kwargs)

    monkeypatch.setattr(archive_type, "get_text", record_get_text)
    captured = []
    original_prepare = agent_module.prepare_live_context_snapshot

    async def capture_prepare(**kwargs):
        prepared = await original_prepare(**kwargs)
        captured.append(prepared)
        return prepared

    monkeypatch.setattr(
        agent_module,
        "prepare_live_context_snapshot",
        capture_prepare,
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(ScriptedTransport([{"text": "done"}])),
        memory_hooks=_OversizedOptionalMemoryInstruction(),
    )

    result = asyncio.run(run_agent_task(agent, "skip oversized optional source"))

    assert result.status is LoopStatus.FINISHED
    prepared = captured[0]
    memory_instruction = next(
        item
        for item in prepared.invocation.fact.static_instructions
        if item.source_id == "memory_scope_instruction"
    )
    assert all(
        entry.candidate.source_instance_id != "memory:instruction"
        for entry in prepared.prepared_candidates.entries
    )
    assert memory_instruction.content_artifact_id not in read_artifact_ids


def test_append_revision_has_provider_visible_latest_wins_envelope(
    tmp_path, monkeypatch
) -> None:
    prepared = asyncio.run(_capture_prepared_snapshot(tmp_path, monkeypatch))
    clock = next(
        item
        for item in prepared.invocation.fact.context_source_candidates
        if item.source_id is ContextSourceId.RUNTIME_CLOCK
    )

    rendered = render_context_source_candidate(clock)

    assert rendered.startswith("<context-source-revision>\n")
    assert '"source_instance_id":"runtime:clock"' in rendered
    assert (
        '"selection_rule":"latest_appended_revision_for_source_instance_wins"'
        in rendered
    )
    assert '"supersedes":"all_prior_revisions_for_source_instance"' in rendered
    assert rendered.endswith("</context-source-revision>")


def test_system_prompt_retains_per_source_fragment_ownership(
    tmp_path, monkeypatch
) -> None:
    import pulsara_agent.runtime.agent as agent_module

    compiled = []
    original_compile = agent_module.compile_context_from_facts

    def capture_compile(**kwargs):
        result = original_compile(**kwargs)
        compiled.append(result)
        return result

    monkeypatch.setattr(agent_module, "compile_context_from_facts", capture_compile)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(ScriptedTransport([{"text": "done"}])),
        memory_hooks=_SmallMemoryInstruction(),
    )

    result = asyncio.run(run_agent_task(agent, "preserve system owners"))

    assert result.status is LoopStatus.FINISHED
    final = compiled[-1]
    fragments = final.provider_system_fragments
    assert tuple(item.source_instance_id for item in fragments) == (
        "system:prompt",
        "memory:instruction",
    )
    assert len({item.owner_semantic_fingerprint for item in fragments}) == 2
    assert "\n\n".join(item.rendered_text for item in fragments) == (
        final.llm_context.system_prompt
    )


def test_compiler_omission_is_final_provider_payload_truth(
    tmp_path, monkeypatch
) -> None:
    import pulsara_agent.runtime.agent as agent_module

    compiled = []
    candidate_sets = []
    original_compile = agent_module.compile_context_from_facts

    def capture_compile(**kwargs):
        candidate_sets.append(kwargs["section_candidates"])
        result = original_compile(**kwargs)
        compiled.append(result)
        return result

    monkeypatch.setattr(agent_module, "compile_context_from_facts", capture_compile)
    transport = ScriptedTransport([{"text": "done"}])
    registry = LLMTransportRegistry()
    registry.register(transport)
    limits = test_model_limits(
        total_context_tokens=8_192,
        max_input_tokens=8_192,
        max_output_tokens=512,
        default_output_tokens=512,
        input_safety_margin_tokens=512,
    )
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api="scripted",
        pro_limits=limits,
        flash_limits=limits,
    )
    runtime_session = in_memory_runtime_session(tmp_path)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=LLMRuntime(config=config, registry=registry),
        memory_hooks=_BudgetPressureMemoryInstruction(),
    )

    result = asyncio.run(run_agent_task(agent, "honor compiler source clipping"))

    assert result.status is LoopStatus.FINISHED
    assert any(
        entry.candidate.source_instance_id == "memory:instruction"
        for entry in candidate_sets[-1].entries
    )
    memory_section = next(
        item for item in compiled[-1].sections if item.id == "memory:instruction"
    )
    assert memory_section.included is False
    assert memory_section.render_mode == "omitted"
    wire_context = transport.contexts[0]
    assert "PROVIDER_SOURCE_MUST_BE_OMITTED_" not in (wire_context.system_prompt or "")
    compiled_event = next(
        item
        for item in runtime_session.event_log.iter()
        if isinstance(item, ContextCompiledEvent) and item.status == "compiled"
    )
    assert (
        compiled_event.budget.final_payload_estimated_tokens
        == wire_context.compiler_estimated_input_tokens
    )


def test_provider_units_preserve_exact_compiler_source_authority(
    tmp_path, monkeypatch
) -> None:
    import pulsara_agent.runtime.agent as agent_module

    compiled = []
    bundles = []
    original_compile = agent_module.compile_context_from_facts
    runtime_session = in_memory_runtime_session(tmp_path)
    coordinator = runtime_session.provider_input_generation_coordinator
    original_prepare = coordinator.prepare_compiled_call

    def capture_compile(**kwargs):
        result = original_compile(**kwargs)
        compiled.append(result)
        return result

    async def capture_bundle(**kwargs):
        result = await original_prepare(**kwargs)
        bundles.append(result)
        return result

    monkeypatch.setattr(agent_module, "compile_context_from_facts", capture_compile)
    monkeypatch.setattr(coordinator, "prepare_compiled_call", capture_bundle)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(ScriptedTransport([{"text": "done"}])),
        memory_hooks=_SmallMemoryInstruction(),
    )

    result = asyncio.run(run_agent_task(agent, "preserve exact provider authority"))

    assert result.status is LoopStatus.FINISHED
    final = compiled[-1]
    units = bundles[-1].resident.units
    by_owner = {item.attribution.owner_semantic_fingerprint: item for item in units}
    for fragment in final.provider_source_fragments:
        unit = by_owner[fragment.owner_semantic_fingerprint]
        candidate = fragment.candidate
        assert (
            unit.attribution.source_event_refs
            == candidate.attribution.source_event_refs
        )
        assert (
            unit.attribution.source_artifact_refs
            == candidate.attribution.source_artifact_refs
        )
        assert (
            unit.attribution.authority_horizons
            == candidate.attribution.authority_horizons
        )
    transcript_units = tuple(
        item
        for item in units
        if item.attribution.semantic.unit_kind == "transcript_message"
    )
    assert transcript_units
    assert all(item.attribution.source_event_refs for item in transcript_units)
    for unit in transcript_units:
        ref_owners = {
            item.runtime_session_id for item in unit.attribution.source_event_refs
        }
        horizon_owners = {
            item.runtime_session_id for item in unit.attribution.authority_horizons
        }
        assert horizon_owners == ref_owners


def test_append_event_rejects_outer_horizon_drift(tmp_path) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(ScriptedTransport([{"text": "done"}])),
    )
    result = asyncio.run(run_agent_task(agent, "freeze append horizons"))
    assert result.status is LoopStatus.FINISHED
    append = next(
        event
        for event in runtime_session.event_log.iter()
        if isinstance(event, ProviderInputAppendCommittedEvent)
    )
    payload = append.model_dump(mode="python")
    payload["authority_horizons"] = ()

    with pytest.raises(ValueError, match="append transition drifted"):
        ProviderInputAppendCommittedEvent.model_validate(payload)


def test_inspector_generation_projection_uses_session_history_across_runs(
    tmp_path,
) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(
            ScriptedTransport([{"text": "first"}, {"text": "second"}])
        ),
    )
    first = asyncio.run(run_agent_task(agent, "first provider generation run"))
    second = asyncio.run(run_agent_task(agent, "second provider generation run"))
    assert first.status is LoopStatus.FINISHED
    assert second.status is LoopStatus.FINISHED
    events = tuple(runtime_session.event_log.iter())
    run_starts = tuple(item for item in events if isinstance(item, RunStartEvent))
    assert len(run_starts) == 2
    second_run_events = tuple(
        item for item in events if item.run_id == run_starts[-1].run_id
    )
    generation_ids = _referenced_provider_input_generation_ids(second_run_events)
    assert generation_ids

    session_projection = _provider_input_generation_projection(
        events,
        include_generation_ids=generation_ids,
    )
    run_only_projection = _provider_input_generation_projection(
        second_run_events,
        include_generation_ids=generation_ids,
    )

    assert {
        item["generation_id"] for item in session_projection["generations"]
    } == generation_ids
    assert all(
        item["generation"] is not None and item["exact_replay_status"] == "exact_replay"
        for item in session_projection["generations"]
    )
    missing_run_local_starts = tuple(
        item
        for item in run_only_projection["generations"]
        if item["generation"] is None
    )
    assert missing_run_local_starts
    assert all(
        item["exact_replay_status"] != "exact_replay"
        for item in missing_run_local_starts
    )


def test_provider_vector_append_path_copies_at_most_five_leaves() -> None:
    initial_units = tuple(_message_unit(index) for index in range(257))
    append_units = tuple(_message_unit(index) for index in range(257, 769))
    initial = prepare_provider_input_vector(initial_units)

    appended = append_provider_input_vector(initial.state, append_units)
    rebuilt = prepare_provider_input_vector((*initial_units, *append_units))

    changed_leaves = tuple(
        item for item in appended.changed_node_references if item.node_kind == "leaf"
    )
    assert len(changed_leaves) == MAX_CHANGED_LEAVES == 5
    assert len(appended.changed_node_references) <= MAX_CHANGED_NODES
    assert appended.state.levels[0][:2] == initial.state.levels[0][:2]
    assert appended.root_reference == rebuilt.root_reference
    assert append_carrier(
        hydrate_carrier(initial_units), append_units
    ) == hydrate_carrier((*initial_units, *append_units))


def test_provider_vector_accepts_512_units_and_rejects_513() -> None:
    initial = prepare_provider_input_vector((_message_unit(0),))
    maximum = tuple(
        _message_unit(index) for index in range(1, APPEND_MAX_UNITS + 1)
    )

    appended = append_provider_input_vector(initial.state, maximum)

    assert len(appended.units) == APPEND_MAX_UNITS + 1
    with pytest.raises(ValueError, match="append exceeds hard bound"):
        append_provider_input_vector(
            initial.state,
            (*maximum, _message_unit(APPEND_MAX_UNITS + 1)),
        )


def test_provider_append_canonical_byte_bound_accepts_limit_and_rejects_overflow() -> None:
    policy = build_default_resolved_causal_physical_policy()
    _validate_append_artifact_physical_bound(
        canonical_text="x" * policy.max_append_candidate_canonical_bytes,
        max_canonical_bytes=policy.max_append_candidate_canonical_bytes,
    )

    with pytest.raises(ValueError, match="canonical-byte bound"):
        _validate_append_artifact_physical_bound(
            canonical_text="x" * (policy.max_append_candidate_canonical_bytes + 1),
            max_canonical_bytes=policy.max_append_candidate_canonical_bytes,
        )


def test_tool_loop_projection_is_causal_and_strict_prefix(tmp_path, monkeypatch) -> None:
    import pulsara_agent.runtime.agent as agent_module

    compiled = []
    plans = []
    original_compile = agent_module.compile_context_from_facts
    runtime_session = in_memory_runtime_session(tmp_path)
    coordinator = runtime_session.provider_input_generation_coordinator
    original_prepare = coordinator.prepare_compiled_call

    def capture_compile(**kwargs):
        result = original_compile(**kwargs)
        compiled.append(result)
        return result

    async def capture_plan(**kwargs):
        result = await original_prepare(**kwargs)
        plans.append(result)
        return result

    monkeypatch.setattr(agent_module, "compile_context_from_facts", capture_compile)
    monkeypatch.setattr(coordinator, "prepare_compiled_call", capture_plan)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(
            ScriptedTransport(
                [
                    {
                        "tool_calls": [
                            {"id": "call:causal", "name": "noop", "arguments": "{}"}
                        ]
                    },
                    {"text": "done"},
                ]
            )
        ),
    )

    result = asyncio.run(run_agent_task(agent, "exercise causal tool order"))

    assert result.status is LoopStatus.FINISHED
    projections = _distinct_ordered_projections(compiled)
    assert len(projections) == 2
    before, after = projections
    before_semantics = tuple(
        item.unit_causal_semantic_fingerprint for item in before.ordered_units
    )
    after_semantics = tuple(
        item.unit_causal_semantic_fingerprint for item in after.ordered_units
    )
    assert before_semantics == after_semantics[: len(before_semantics)]
    assert before.ordered_units[0].causal_placement == (
        after.ordered_units[0].causal_placement
    )
    assert tuple(
        item.wire_semantic.provider_message.role for item in after.ordered_units
    ) == ("user", "assistant", "tool_result")
    tool_result = after.ordered_units[-1]
    assert tool_result.causal_placement.visible_causal_predecessor_node_identity_fingerprints == (
        after.ordered_units[-2]
        .causal_placement.node_identity.node_identity_fingerprint,
    )
    first_frame = plans[0].prepared_plan.frame_placement
    assert first_frame is not None
    assert first_frame.insertion_kind == "before_new_current_user"
    assert first_frame.following_transcript_node_identity_fingerprint == (
        before.ordered_units[0]
        .causal_placement.node_identity.node_identity_fingerprint
    )


def test_cross_run_classification_does_not_change_prefix_semantic(
    tmp_path, monkeypatch
) -> None:
    import pulsara_agent.runtime.agent as agent_module

    compiled = []
    original_compile = agent_module.compile_context_from_facts

    def capture_compile(**kwargs):
        result = original_compile(**kwargs)
        compiled.append(result)
        return result

    monkeypatch.setattr(agent_module, "compile_context_from_facts", capture_compile)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(
            ScriptedTransport([{"text": "first"}, {"text": "second"}])
        ),
    )

    first = asyncio.run(run_agent_task(agent, "first causal run"))
    second = asyncio.run(run_agent_task(agent, "second causal run"))

    assert first.status is second.status is LoopStatus.FINISHED
    projections = _distinct_ordered_projections(compiled)
    assert len(projections) == 2
    first_user = projections[0].ordered_units[0]
    retained_first_user = projections[1].ordered_units[0]
    assert first_user.wire_semantic == retained_first_user.wire_semantic
    assert first_user.causal_placement == retained_first_user.causal_placement
    assert first_user.unit_causal_semantic_fingerprint == (
        retained_first_user.unit_causal_semantic_fingerprint
    )
    assert first_user.invocation_attribution.invocation_classification == "current_user"
    assert (
        retained_first_user.invocation_attribution.invocation_classification
        == "prior_history"
    )
    assert tuple(
        item.wire_semantic.provider_message.role
        for item in projections[1].ordered_units
    ) == ("user", "assistant", "user")


def test_accepted_continuation_commits_exact_materialization_proof(tmp_path) -> None:
    transport = ScriptedTransport(
        [
            {"tool_calls": [{"id": "call:noop", "name": "noop", "arguments": "{}"}]},
            {"text": "done"},
        ]
    )
    runtime_session = in_memory_runtime_session(tmp_path)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(transport),
    )

    result = asyncio.run(run_agent_task(agent, "use noop"))

    assert result.status is LoopStatus.FINISHED
    append = next(
        event
        for event in runtime_session.event_log.iter()
        if isinstance(event, ProviderInputAppendCommittedEvent)
        and event.continuation_materialization_proof is not None
    )
    proof = append.continuation_materialization_proof
    assert proof is not None
    assert (
        proof.pending_continuation_fingerprint
        == append.consumed_pending_continuation_fingerprint
    )
    assert proof.appended_unit_ordinals
    assert len(proof.appended_unit_ordinals) == len(
        proof.ordered_appended_unit_materialization_fingerprints
    )


def test_runtime_session_close_durably_closes_open_provider_generation(
    tmp_path,
) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(ScriptedTransport([{"text": "done"}])),
    )
    result = asyncio.run(run_agent_task(agent, "close generation"))
    assert result.status is LoopStatus.FINISHED
    assert (
        runtime_session.provider_input_generation_store.open_session_continuity_snapshots()
    )

    runtime_session.close()

    closes = tuple(
        event
        for event in runtime_session.event_log.iter()
        if isinstance(event, ProviderInputGenerationClosedEvent)
        and event.close_reason == "session_close"
    )
    assert len(closes) == 1
    assert (
        runtime_session.provider_input_generation_store.open_session_continuity_snapshots()
        == ()
    )
