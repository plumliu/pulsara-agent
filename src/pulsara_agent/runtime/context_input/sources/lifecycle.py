"""Authoritative runtime-observation producer and source lifecycle registry."""

from __future__ import annotations

from functools import lru_cache

from pulsara_agent.llm.user_carrier import (
    MAX_USER_CARRIER_WIRE_BYTES,
    ROOT_USER_CARRIER_INTERPRETATION_FINGERPRINT,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.context_source import (
    ActiveSkillPayloadFact,
    CapabilityCatalogPayloadFact,
    ContextSourceId,
    McpDiagnosticPayloadFact,
    MemoryProjectionPayloadFact,
    PlanRevisionPayloadFact,
    RolloutStatusPayloadFact,
    WorkspaceSkillPayloadFact,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.runtime_observation import (
    ContextSourceLifecycleRegistryContractFact,
    ContextSourceLifecycleRegistryEntryFact,
    ContextSourceObservationKindBindingFact,
    ContextSourceObservationProducerFact,
    HumanInputProtocolContractFact,
    LongHorizonRewriteObservationProducerFact,
    ProviderUserCarrierProtocolContractFact,
    RuntimeObservationCanonicalCodecContractFact,
    RuntimeObservationKindContractFact,
    RuntimeObservationProtocolContractFact,
    RuntimeRequestKindContractFact,
    RuntimeRequestProtocolContractFact,
    TranscriptLifecycleObservationProducerFact,
)


_SOURCE_LIFECYCLES: dict[ContextSourceId, dict[str, object]] = {
    ContextSourceId.SYSTEM: dict(
        lifecycle_class="generation_root",
        source_instance_scope="runtime_session",
        absence_semantics="forbidden",
        closure_kind="root_rollover",
        rollover_materialization="rebuild_root_from_exact_reference",
        rewrite_eligibility="never",
        bindings=(),
    ),
    ContextSourceId.RUNTIME_ENVIRONMENT: dict(
        lifecycle_class="generation_root",
        source_instance_scope="runtime_session",
        absence_semantics="forbidden",
        closure_kind="root_rollover",
        rollover_materialization="rebuild_root_from_exact_reference",
        rewrite_eligibility="never",
        bindings=(),
    ),
    ContextSourceId.MEMORY_INSTRUCTION: dict(
        lifecycle_class="generation_root",
        source_instance_scope="runtime_session",
        absence_semantics="retain_effective_head",
        closure_kind="root_rollover",
        rollover_materialization="rebuild_root_from_exact_reference",
        rewrite_eligibility="never",
        bindings=(),
    ),
    ContextSourceId.RUNTIME_CLOCK: dict(
        lifecycle_class="causal_append_once",
        source_instance_scope="model_call",
        absence_semantics="no_new_fact",
        closure_kind="none",
        rollover_materialization="copy_immutable_causal_unit",
        rewrite_eligibility="long_horizon_rewrite",
        bindings=(("observation", "runtime_clock"),),
    ),
    ContextSourceId.MEMORY_PROJECTION: dict(
        lifecycle_class="replacement_snapshot",
        source_instance_scope="continuity_cohort",
        absence_semantics="retain_effective_head",
        closure_kind="empty_replacement",
        rollover_materialization="reuse_effective_snapshot_reference",
        rewrite_eligibility="superseded_only",
        bindings=(
            ("explicit_empty", "recalled_memory_snapshot"),
            ("snapshot_update", "recalled_memory_snapshot"),
        ),
    ),
    ContextSourceId.CAPABILITY_CATALOG: dict(
        lifecycle_class="replacement_snapshot",
        source_instance_scope="runtime_session",
        absence_semantics="retain_effective_head",
        closure_kind="empty_replacement",
        rollover_materialization="reuse_effective_snapshot_reference",
        rewrite_eligibility="superseded_only",
        bindings=(
            ("explicit_empty", "capability_prose_snapshot"),
            ("snapshot_update", "capability_prose_snapshot"),
        ),
    ),
    ContextSourceId.ACTIVE_SKILL: dict(
        lifecycle_class="replacement_snapshot",
        source_instance_scope="continuity_cohort",
        absence_semantics="retain_effective_head",
        closure_kind="empty_replacement",
        rollover_materialization="reuse_effective_snapshot_reference",
        rewrite_eligibility="superseded_only",
        bindings=(
            ("explicit_empty", "active_skill_snapshot"),
            ("snapshot_update", "active_skill_snapshot"),
        ),
    ),
    ContextSourceId.WORKSPACE_SKILL: dict(
        lifecycle_class="replacement_snapshot",
        source_instance_scope="runtime_session",
        absence_semantics="retain_effective_head",
        closure_kind="empty_replacement",
        rollover_materialization="reuse_effective_snapshot_reference",
        rewrite_eligibility="superseded_only",
        bindings=(
            ("explicit_empty", "workspace_skill_snapshot"),
            ("snapshot_update", "workspace_skill_snapshot"),
        ),
    ),
    ContextSourceId.PLAN_STATUS: dict(
        lifecycle_class="replacement_snapshot",
        source_instance_scope="workflow",
        absence_semantics="retain_effective_head",
        closure_kind="typed_terminal_snapshot",
        rollover_materialization="reuse_effective_snapshot_reference",
        rewrite_eligibility="superseded_only",
        bindings=(
            ("snapshot_update", "plan_status_snapshot"),
            ("terminal", "plan_status_snapshot"),
        ),
    ),
    ContextSourceId.PLAN_GUIDANCE: dict(
        lifecycle_class="causal_append_once",
        source_instance_scope="workflow",
        absence_semantics="no_new_fact",
        closure_kind="none",
        rollover_materialization="copy_immutable_causal_unit",
        rewrite_eligibility="after_causal_close",
        bindings=(("guidance", "plan_guidance"),),
    ),
    ContextSourceId.RECOVERY: dict(
        lifecycle_class="causal_append_once",
        source_instance_scope="run",
        absence_semantics="no_new_fact",
        closure_kind="none",
        rollover_materialization="copy_immutable_causal_unit",
        rewrite_eligibility="after_causal_close",
        bindings=(("guidance", "recovery_guidance"),),
    ),
    ContextSourceId.ROLLOUT_STATUS: dict(
        lifecycle_class="replacement_snapshot",
        source_instance_scope="run",
        absence_semantics="retain_effective_head",
        closure_kind="typed_terminal_snapshot",
        rollover_materialization="reuse_effective_snapshot_reference",
        rewrite_eligibility="superseded_only",
        bindings=(
            ("status_update", "rollout_status_snapshot"),
            ("terminal", "rollout_status_snapshot"),
        ),
    ),
    ContextSourceId.SUBAGENT_HANDOFF: dict(
        lifecycle_class="causal_append_once",
        source_instance_scope="subagent",
        absence_semantics="no_new_fact",
        closure_kind="none",
        rollover_materialization="copy_immutable_causal_unit",
        rewrite_eligibility="after_causal_close",
        bindings=(("handoff", "subagent_handoff"),),
    ),
    ContextSourceId.SUBAGENT_RESULT: dict(
        lifecycle_class="immutable_append_once",
        source_instance_scope="subagent",
        absence_semantics="no_new_fact",
        closure_kind="none",
        rollover_materialization="copy_immutable_causal_unit",
        rewrite_eligibility="long_horizon_rewrite",
        bindings=(("delivery", "subagent_result_delivery"),),
    ),
    ContextSourceId.MCP_DIAGNOSTIC: dict(
        lifecycle_class="replacement_snapshot",
        source_instance_scope="runtime_session",
        absence_semantics="retain_effective_head",
        closure_kind="typed_terminal_snapshot",
        rollover_materialization="reuse_effective_snapshot_reference",
        rewrite_eligibility="superseded_only",
        bindings=(
            ("diagnostic_update", "mcp_diagnostic_snapshot"),
            ("terminal", "mcp_diagnostic_snapshot"),
        ),
    ),
}


@lru_cache(maxsize=1)
def default_context_source_lifecycle_registry() -> (
    ContextSourceLifecycleRegistryContractFact
):
    if set(_SOURCE_LIFECYCLES) != set(ContextSourceId):
        raise ValueError("ContextSource lifecycle registry is not exhaustive")
    entries = []
    for source_id in sorted(ContextSourceId, key=lambda item: item.value):
        spec = _SOURCE_LIFECYCLES[source_id]
        bindings = tuple(
            build_frozen_fact(
                ContextSourceObservationKindBindingFact,
                schema_version="context_source_observation_kind_binding.v1",
                transition_kind=transition,
                observation_kind=kind,
            )
            for transition, kind in sorted(spec["bindings"])
        )
        entries.append(
            build_frozen_fact(
                ContextSourceLifecycleRegistryEntryFact,
                schema_version="context_source_lifecycle_registry_entry.v2",
                source_id=source_id,
                lifecycle_class=spec["lifecycle_class"],
                source_instance_scope=spec["source_instance_scope"],
                absence_semantics=spec["absence_semantics"],
                closure_kind=spec["closure_kind"],
                rollover_materialization=spec["rollover_materialization"],
                rewrite_eligibility=spec["rewrite_eligibility"],
                observation_kind_bindings=bindings,
            )
        )
    return build_frozen_fact(
        ContextSourceLifecycleRegistryContractFact,
        schema_version="context_source_lifecycle_registry_contract.v2",
        registry_id="pulsara.context-source-lifecycle",
        registry_version="2",
        ordered_entries=tuple(entries),
    )


def _context_source_producer(
    source_id: ContextSourceId,
    transition_kind: str,
):
    return build_frozen_fact(
        ContextSourceObservationProducerFact,
        schema_version="context_source_observation_producer.v1",
        source_id=source_id,
        transition_kind=transition_kind,
    )


def _kind_policy(kind: str) -> tuple[str, str, str, str, str]:
    if kind in {"runtime_clock", "lifecycle_observation"}:
        return (
            "runtime_fact",
            "causal_append_once",
            "long_horizon_rewrite",
            "protect_current_run",
            "fact_only_not_instruction",
        )
    if kind == "plan_guidance":
        return (
            "runtime_guidance",
            "causal_append_once",
            "after_causal_close",
            "protect_until_closed",
            "runtime_guidance_under_root_policy",
        )
    if kind in {"recovery_guidance", "subagent_handoff"}:
        return (
            "runtime_fact_and_guidance",
            "causal_append_once",
            "after_causal_close",
            "protect_until_closed",
            "typed_fact_with_bounded_guidance",
        )
    if kind == "active_skill_snapshot":
        return (
            "runtime_guidance",
            "replacement_snapshot",
            "superseded_only",
            "protect_effective_head",
            "runtime_guidance_under_root_policy",
        )
    if kind == "subagent_result_delivery":
        return (
            "runtime_fact",
            "immutable_append_once",
            "long_horizon_rewrite",
            "protect_current_run",
            "fact_only_not_instruction",
        )
    if kind == "runtime_observation_rewrite_projection":
        return (
            "runtime_fact",
            "immutable_append_once",
            "long_horizon_rewrite",
            "protect_current_run",
            "fact_only_not_instruction",
        )
    if kind in {
        "compaction_replacement_summary",
        "long_horizon_rollup_observation",
    }:
        return (
            "runtime_fact",
            "causal_append_once",
            "long_horizon_rewrite",
            "protect_current_run",
            "fact_only_not_instruction",
        )
    authority = (
        "runtime_fact_and_guidance"
        if kind
        in {
            "capability_prose_snapshot",
            "workspace_skill_snapshot",
        }
        else "runtime_fact"
    )
    instruction = (
        "typed_fact_with_bounded_guidance"
        if authority == "runtime_fact_and_guidance"
        else "fact_only_not_instruction"
    )
    return (
        authority,
        "replacement_snapshot",
        "superseded_only",
        "protect_effective_head",
        instruction,
    )


_KIND_PAYLOAD_SCHEMA = {
    "runtime_clock": "runtime_clock_observation_payload.v2",
    "recalled_memory_snapshot": "context_source_replacement_observation_payload.v1",
    "capability_prose_snapshot": "context_source_replacement_observation_payload.v1",
    "active_skill_snapshot": "context_source_replacement_observation_payload.v1",
    "workspace_skill_snapshot": "context_source_replacement_observation_payload.v1",
    "plan_status_snapshot": "context_source_replacement_observation_payload.v1",
    "plan_guidance": "context_source_append_observation_payload.v1",
    "recovery_guidance": "context_source_append_observation_payload.v1",
    "rollout_status_snapshot": "context_source_replacement_observation_payload.v1",
    "subagent_handoff": "context_source_append_observation_payload.v1",
    "subagent_result_delivery": "context_source_append_observation_payload.v1",
    "mcp_diagnostic_snapshot": "context_source_replacement_observation_payload.v1",
    "lifecycle_observation": "transcript_lifecycle_observation_payload.v1",
    "compaction_replacement_summary": "derived_text_runtime_observation_payload.v1",
    "long_horizon_rollup_observation": "derived_text_runtime_observation_payload.v1",
    "runtime_observation_rewrite_projection": (
        "runtime_observation_rewrite_projection_payload.v2"
    ),
}


@lru_cache(maxsize=1)
def default_runtime_observation_protocol() -> RuntimeObservationProtocolContractFact:
    lifecycle = default_context_source_lifecycle_registry()
    source_kind_producers: dict[
        str, list[ContextSourceObservationProducerFact]
    ] = {}
    for source_id, spec in _SOURCE_LIFECYCLES.items():
        if not spec["bindings"]:
            continue
        for transition, kind in spec["bindings"]:
            producer = _context_source_producer(source_id, transition)
            source_kind_producers.setdefault(kind, []).append(producer)
    transcript_producer = build_frozen_fact(
        TranscriptLifecycleObservationProducerFact,
        schema_version="transcript_lifecycle_observation_producer.v1",
        event_domain_contract_id="pulsara.transcript-lifecycle-observation",
        event_domain_contract_version="1",
        event_domain_contract_fingerprint=context_fingerprint(
            "transcript-lifecycle-event-domain:v1", "RunEnd+TerminalProcessCompleted"
        ),
        supported_source_event_contract_set_fingerprint=context_fingerprint(
            "transcript-lifecycle-supported-events:v1",
            ("RunEndEvent", "TerminalProcessCompletedEvent"),
        ),
        reducer_contract_fingerprint=context_fingerprint(
            "transcript-lifecycle-reducer:v1", "typed-causal-append"
        ),
    )
    rewrite_producer = build_frozen_fact(
        LongHorizonRewriteObservationProducerFact,
        schema_version="long_horizon_rewrite_observation_producer.v1",
        rewrite_contract_id="pulsara.runtime-observation-long-horizon-rewrite",
        rewrite_contract_version="1",
        rewrite_contract_fingerprint=context_fingerprint(
            "runtime-observation-long-horizon-rewrite:v1", "typed-bounded-proof"
        ),
    )
    producers: dict[str, tuple[object, ...]] = {
        kind: tuple(items) for kind, items in source_kind_producers.items()
    }
    producers.update(
        {
            "lifecycle_observation": (transcript_producer,),
            "compaction_replacement_summary": (rewrite_producer,),
            "long_horizon_rollup_observation": (rewrite_producer,),
            "runtime_observation_rewrite_projection": (rewrite_producer,),
        }
    )
    kind_contracts = []
    for kind, kind_producers in sorted(producers.items()):
        authority, lifecycle_class, rewrite, protection, instruction = _kind_policy(
            kind
        )
        kind_contracts.append(
            build_frozen_fact(
                RuntimeObservationKindContractFact,
                schema_version="runtime_observation_kind_contract.v1",
                kind=kind,
                producers=tuple(
                    sorted(
                        kind_producers,
                        key=lambda item: item.producer_fingerprint,
                    )
                ),
                authority_class=authority,
                lifecycle_class=lifecycle_class,
                payload_schema_version=_KIND_PAYLOAD_SCHEMA[kind],
                payload_schema_fingerprint=context_fingerprint(
                    "runtime-observation-payload-schema:v2",
                    _KIND_PAYLOAD_SCHEMA[kind],
                ),
                maximum_payload_utf8_bytes=MAX_USER_CARRIER_WIRE_BYTES,
                rewrite_eligibility=rewrite,
                protection_policy=protection,
                instruction_policy=instruction,
            )
        )
    codec = build_frozen_fact(
        RuntimeObservationCanonicalCodecContractFact,
        schema_version="runtime_observation_canonical_codec_contract.v1",
        codec_id="pulsara.runtime-user-carrier.canonical-json",
        codec_version="1",
        encoding="utf-8",
        object_key_order="lexicographic",
        unicode_normalization="NFC",
        string_escaping="json",
        non_finite_numbers="forbidden",
        unknown_fields="forbidden",
        maximum_wire_utf8_bytes=MAX_USER_CARRIER_WIRE_BYTES,
    )
    return build_frozen_fact(
        RuntimeObservationProtocolContractFact,
        schema_version="runtime_observation_protocol_contract.v2",
        protocol_id="pulsara.runtime-observation",
        protocol_version="2",
        wire_role="user",
        codec_contract=codec,
        ordered_kind_contracts=tuple(kind_contracts),
        source_lifecycle_registry_contract_fingerprint=lifecycle.registry_fingerprint,
        unknown_kind_policy="reject_before_adapter",
        unknown_contract_policy="reject_before_adapter",
    )


@lru_cache(maxsize=1)
def default_runtime_request_protocol() -> RuntimeRequestProtocolContractFact:
    matrix = {
        "subagent_task": (
            "child_run_entry",
            "persist_child_canonical_transcript",
            ("subagent_spawn",),
        ),
        "current_run_task": (
            "current_run_transcript",
            "persist_current_run_canonical_transcript",
            ("current_run",),
        ),
        "compaction_request": (
            "one_shot_invocation",
            "invocation_scoped_only",
            ("compaction_operation",),
        ),
        "window_compaction_request": (
            "one_shot_invocation",
            "invocation_scoped_only",
            ("window_compaction_operation",),
        ),
        "governance_request": (
            "one_shot_invocation",
            "invocation_scoped_only",
            ("governance_batch",),
        ),
        "reflection_request": (
            "one_shot_invocation",
            "invocation_scoped_only",
            ("reflection_job",),
        ),
        "summarizer_request": (
            "one_shot_invocation",
            "invocation_scoped_only",
            ("summarizer_operation",),
        ),
    }
    kinds = tuple(
        build_frozen_fact(
            RuntimeRequestKindContractFact,
            schema_version="runtime_request_kind_contract.v1",
            request_kind=kind,
            instruction_policy="task_under_root_policy",
            lifecycle_class=values[0],
            transcript_persistence=values[1],
            allowed_owner_kinds=values[2],
            payload_schema_version=(
                "runtime_task_request_payload.v1"
                if kind in {"subagent_task", "current_run_task"}
                else "runtime_operation_request_payload.v1"
            ),
            payload_schema_fingerprint=context_fingerprint(
                "runtime-request-payload-schema:v1", kind
            ),
            maximum_payload_utf8_bytes=MAX_USER_CARRIER_WIRE_BYTES,
            observation_rewrite_policy="never",
        )
        for kind, values in sorted(matrix.items())
    )
    return build_frozen_fact(
        RuntimeRequestProtocolContractFact,
        schema_version="runtime_request_protocol_contract.v1",
        protocol_id="pulsara.runtime-request",
        protocol_version="1",
        wire_role="user",
        envelope_key="pulsara_runtime_request",
        codec_contract_fingerprint=(
            default_runtime_observation_protocol().codec_contract.codec_contract_fingerprint
        ),
        ordered_kind_contracts=kinds,
        unknown_kind_policy="reject_before_adapter",
        unknown_contract_policy="reject_before_adapter",
    )


@lru_cache(maxsize=1)
def default_provider_user_carrier_protocol() -> ProviderUserCarrierProtocolContractFact:
    observation = default_runtime_observation_protocol()
    human = build_frozen_fact(
        HumanInputProtocolContractFact,
        schema_version="human_input_protocol_contract.v1",
        protocol_id="pulsara.human-input",
        protocol_version="1",
        wire_role="user",
        envelope_key="pulsara_human_input",
        codec_contract_fingerprint=observation.codec_contract.codec_contract_fingerprint,
        raw_text_policy="escaped_typed_text_field_only",
        unsupported_multimodal_policy="reject_until_typed_block_contract",
        maximum_text_utf8_bytes=MAX_USER_CARRIER_WIRE_BYTES,
    )
    return build_frozen_fact(
        ProviderUserCarrierProtocolContractFact,
        schema_version="provider_user_carrier_protocol_contract.v2",
        human_input_protocol=human,
        runtime_request_protocol=default_runtime_request_protocol(),
        runtime_observation_protocol=observation,
        root_interpretation_fragment_semantic_fingerprint=(
            ROOT_USER_CARRIER_INTERPRETATION_FINGERPRINT
        ),
        user_item_policy="exactly_one_registered_outer_envelope",
    )


def runtime_observation_kind_contract(kind: str) -> RuntimeObservationKindContractFact:
    matches = tuple(
        item
        for item in default_runtime_observation_protocol().ordered_kind_contracts
        if item.kind == kind
    )
    if len(matches) != 1:
        raise ValueError(f"runtime observation kind is not registered: {kind}")
    return matches[0]


def runtime_observation_context_source_producer(
    *,
    observation_kind: str,
    source_id: ContextSourceId,
    transition_kind: str,
) -> ContextSourceObservationProducerFact:
    matches = tuple(
        item
        for item in runtime_observation_kind_contract(observation_kind).producers
        if isinstance(item, ContextSourceObservationProducerFact)
        and item.source_id is source_id
        and item.transition_kind == transition_kind
    )
    if len(matches) != 1:
        raise ValueError("runtime observation ContextSource producer is ambiguous")
    return matches[0]


def runtime_observation_derived_producer(
    *,
    observation_kind: str,
    producer_kind: str,
):
    matches = tuple(
        item
        for item in runtime_observation_kind_contract(observation_kind).producers
        if item.producer_kind == producer_kind
    )
    if len(matches) != 1:
        raise ValueError("runtime observation derived producer is ambiguous")
    return matches[0]


def context_source_lifecycle_entry(
    source_id: ContextSourceId,
) -> ContextSourceLifecycleRegistryEntryFact:
    matches = tuple(
        item
        for item in default_context_source_lifecycle_registry().ordered_entries
        if item.source_id is source_id
    )
    if len(matches) != 1:
        raise ValueError(f"ContextSource lifecycle is not registered: {source_id.value}")
    return matches[0]


def context_source_transition_kind(source_id: ContextSourceId, payload) -> str:
    if source_id is ContextSourceId.RUNTIME_CLOCK:
        transition = "observation"
    elif source_id is ContextSourceId.MEMORY_PROJECTION:
        if not isinstance(payload, MemoryProjectionPayloadFact):
            raise ValueError("memory source payload contract mismatch")
        transition = (
            "explicit_empty"
            if not payload.ordered_memory_semantic_fingerprints
            else "snapshot_update"
        )
    elif source_id is ContextSourceId.CAPABILITY_CATALOG:
        if not isinstance(payload, CapabilityCatalogPayloadFact):
            raise ValueError("capability source payload contract mismatch")
        transition = (
            "explicit_empty"
            if not payload.ordered_projection_entry_semantic_fingerprints
            else "snapshot_update"
        )
    elif source_id is ContextSourceId.ACTIVE_SKILL:
        if not isinstance(payload, ActiveSkillPayloadFact):
            raise ValueError("active-skill source payload contract mismatch")
        transition = (
            "snapshot_update"
            if payload.ordered_active_skill_semantic_fingerprints
            else "explicit_empty"
        )
    elif source_id is ContextSourceId.WORKSPACE_SKILL:
        if not isinstance(payload, WorkspaceSkillPayloadFact):
            raise ValueError("workspace-skill source payload contract mismatch")
        transition = (
            "explicit_empty"
            if not payload.ordered_skill_semantic_fingerprints
            else "snapshot_update"
        )
    elif source_id is ContextSourceId.PLAN_STATUS:
        if not isinstance(payload, PlanRevisionPayloadFact):
            raise ValueError("plan-status source payload contract mismatch")
        transition = "snapshot_update" if payload.active else "terminal"
    elif source_id is ContextSourceId.PLAN_GUIDANCE:
        transition = "guidance"
    elif source_id is ContextSourceId.RECOVERY:
        transition = "guidance"
    elif source_id is ContextSourceId.ROLLOUT_STATUS:
        if not isinstance(payload, RolloutStatusPayloadFact):
            raise ValueError("rollout source payload contract mismatch")
        transition = "terminal" if payload.phase == "exhausted" else "status_update"
    elif source_id is ContextSourceId.SUBAGENT_HANDOFF:
        transition = "handoff"
    elif source_id is ContextSourceId.SUBAGENT_RESULT:
        transition = "delivery"
    elif source_id is ContextSourceId.MCP_DIAGNOSTIC:
        if not isinstance(payload, McpDiagnosticPayloadFact):
            raise ValueError("MCP diagnostic source payload contract mismatch")
        transition = (
            "terminal"
            if payload.ordered_entries
            and all(item.status in {"failed", "disabled"} for item in payload.ordered_entries)
            else "diagnostic_update"
        )
    else:
        raise ValueError(f"ContextSource does not emit observations: {source_id.value}")
    entry = context_source_lifecycle_entry(source_id)
    matches = tuple(
        item
        for item in entry.observation_kind_bindings
        if item.transition_kind == transition
    )
    if len(matches) != 1:
        raise ValueError("ContextSource transition has no unique observation binding")
    return transition


def context_source_observation_kind(
    source_id: ContextSourceId,
    payload,
) -> str:
    transition = context_source_transition_kind(source_id, payload)
    entry = context_source_lifecycle_entry(source_id)
    return next(
        item.observation_kind
        for item in entry.observation_kind_bindings
        if item.transition_kind == transition
    )


def validate_context_source_observation_binding(
    *,
    source_id: ContextSourceId,
    transition_kind: str,
    observation_kind: str,
) -> None:
    entry = next(
        item
        for item in default_context_source_lifecycle_registry().ordered_entries
        if item.source_id is source_id
    )
    if not any(
        binding.transition_kind == transition_kind
        and binding.observation_kind == observation_kind
        for binding in entry.observation_kind_bindings
    ):
        raise ValueError("ContextSource transition/observation kind binding mismatch")


__all__ = [
    "default_context_source_lifecycle_registry",
    "default_provider_user_carrier_protocol",
    "default_runtime_observation_protocol",
    "default_runtime_request_protocol",
    "context_source_lifecycle_entry",
    "context_source_observation_kind",
    "context_source_transition_kind",
    "runtime_observation_kind_contract",
    "runtime_observation_context_source_producer",
    "runtime_observation_derived_producer",
    "validate_context_source_observation_binding",
]
