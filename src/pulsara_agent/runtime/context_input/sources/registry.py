"""Exact ContextSource contract registry and provider-neutral bindings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pulsara_agent.primitives.context import (
    ContextSectionCandidate,
    context_fingerprint,
)
from pulsara_agent.primitives.context_source import (
    ContextSourceCandidateAttributionFact,
    ContextSourceCandidateSemanticFact,
    ContextSourceContractFact,
    ContextSourceId,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.runtime.context_input.sources.input import (
    CONTEXT_SOURCE_INPUT_TYPES,
    CONTEXT_SOURCE_PAYLOAD_TYPES,
    ContextSourceCollectInput,
    recompute_context_source_input_dependency,
)


class ContextSource(Protocol):
    def collect(
        self,
        *,
        source_input: ContextSourceCollectInput,
    ) -> tuple[ContextSectionCandidate, ...]: ...


@dataclass(frozen=True, slots=True)
class ContextSourceBinding:
    contract: ContextSourceContractFact
    policy: ContextSourceBindingPolicy
    implementation_build_fingerprint: str | None
    source: ContextSource


@dataclass(frozen=True, slots=True)
class ContextSourceBindingPolicy:
    source_id: ContextSourceId
    input_type: type
    payload_type: type
    allowed_lifecycle_kinds: frozenset[str]
    allowed_priorities: frozenset[int]
    allowed_required_values: frozenset[bool]
    allowed_lowering_pairs: frozenset[tuple[str, str | None]]
    policy_fingerprint: str

    def validate(self, source_input: ContextSourceCollectInput) -> None:
        if type(source_input) is not self.input_type:
            raise ValueError("ContextSource input carrier type is unauthorized")
        if type(source_input.payload) is not self.payload_type:
            raise ValueError("ContextSource payload type is unauthorized")
        if source_input.lifecycle.lifecycle_kind not in self.allowed_lifecycle_kinds:
            raise ValueError("ContextSource lifecycle is unauthorized")
        if source_input.priority not in self.allowed_priorities:
            raise ValueError("ContextSource priority is unauthorized")
        if source_input.required not in self.allowed_required_values:
            raise ValueError("ContextSource required policy is unauthorized")
        lowering = (
            source_input.lowering_intent.intent_kind,
            source_input.lowering_intent.role_constraint,
        )
        if lowering not in self.allowed_lowering_pairs:
            raise ValueError("ContextSource lowering intent is unauthorized")


def default_context_source_binding_policy(
    source_id: ContextSourceId,
) -> ContextSourceBindingPolicy:
    lifecycle, priorities, required, lowering = _SOURCE_POLICY_MATRIX[source_id]
    input_type = CONTEXT_SOURCE_INPUT_TYPES[source_id]
    payload_type = CONTEXT_SOURCE_PAYLOAD_TYPES[source_id]
    payload = {
        "source_id": source_id.value,
        "input_type": f"{input_type.__module__}.{input_type.__qualname__}",
        "payload_type": f"{payload_type.__module__}.{payload_type.__qualname__}",
        "lifecycle": tuple(sorted(lifecycle)),
        "priorities": tuple(sorted(priorities)),
        "required": tuple(sorted(required)),
        "lowering": tuple(sorted(lowering)),
    }
    return ContextSourceBindingPolicy(
        source_id=source_id,
        input_type=input_type,
        payload_type=payload_type,
        allowed_lifecycle_kinds=frozenset(lifecycle),
        allowed_priorities=frozenset(priorities),
        allowed_required_values=frozenset(required),
        allowed_lowering_pairs=frozenset(lowering),
        policy_fingerprint=context_fingerprint(
            "context-source-binding-policy:v1", payload
        ),
    )


class CanonicalContextSource:
    """Closed source implementation for already-authorized typed input."""

    def __init__(
        self,
        binding_contract: ContextSourceContractFact,
        binding_policy: ContextSourceBindingPolicy,
    ) -> None:
        self._contract = binding_contract
        self._policy = binding_policy

    def collect(
        self,
        *,
        source_input: ContextSourceCollectInput,
    ) -> tuple[ContextSectionCandidate, ...]:
        authority = source_input.authority
        self._policy.validate(source_input)
        if authority.source_id is not self._contract.source_id:
            raise ValueError("source input discriminator differs from binding")
        if (
            authority.source_contract_version != self._contract.source_version
            or authority.source_contract_fingerprint
            != self._contract.contract_fingerprint
        ):
            raise ValueError("source input contract cannot be rebound")
        if (
            self._contract.binding_policy_fingerprint != self._policy.policy_fingerprint
            or authority.input_dependency_fingerprint
            != recompute_context_source_input_dependency(source_input)
        ):
            raise ValueError("source input authority cannot be caller self-certified")
        semantic = build_frozen_fact(
            ContextSourceCandidateSemanticFact,
            schema_version="context_source_candidate_semantic.v1",
            source_id=self._contract.source_id,
            source_instance_id=source_input.source_instance_id,
            candidate_key=source_input.candidate_key,
            source_revision=source_input.source_revision,
            payload=source_input.payload,
            lifecycle=source_input.lifecycle,
            priority=source_input.priority,
            required=source_input.required,
            lowering_intent=source_input.lowering_intent,
            model_visible_timing_semantic=None,
        )
        attribution = build_frozen_fact(
            ContextSourceCandidateAttributionFact,
            schema_version="context_source_candidate_attribution.v1",
            semantic=semantic,
            source_event_refs=source_input.source_event_refs,
            source_artifact_refs=source_input.source_artifact_refs,
            source_absolute_timing=source_input.source_absolute_timing,
            authority_horizons=authority.authority_horizons,
            source_input_authority_fingerprint=authority.authority_fingerprint,
            physical_input_policy_fingerprint=(
                authority.physical_input_policy_fingerprint
            ),
            source_contract_id=self._contract.source_id.value,
            source_contract_version=self._contract.source_version,
            source_contract_fingerprint=self._contract.contract_fingerprint,
        )
        payload = {
            "schema_version": "context_section_candidate.v2",
            "attribution": attribution,
        }
        return (
            ContextSectionCandidate(
                **payload,
                candidate_fingerprint=context_fingerprint(
                    "context-section-candidate-fact:v2",
                    payload,
                ),
            ),
        )


class ContextSourceRegistry:
    def __init__(self, bindings: tuple[ContextSourceBinding, ...]) -> None:
        ordered = tuple(
            sorted(bindings, key=lambda item: item.contract.source_id.value)
        )
        ids = tuple(item.contract.source_id for item in ordered)
        if len(ids) != len(set(ids)):
            raise ValueError("ContextSource registry IDs are duplicated")
        self._bindings = ordered
        self._by_id = {item.contract.source_id: item for item in ordered}
        self.registry_fingerprint = context_fingerprint(
            "context-source-registry:v1",
            tuple(
                (
                    item.contract.contract_fingerprint,
                    item.policy.policy_fingerprint,
                )
                for item in ordered
            ),
        )

    @property
    def bindings(self) -> tuple[ContextSourceBinding, ...]:
        return self._bindings

    def resolve(
        self,
        *,
        source_id: ContextSourceId,
        source_version: str | None = None,
        contract_fingerprint: str | None = None,
    ) -> ContextSourceBinding:
        try:
            binding = self._by_id[source_id]
        except KeyError as exc:
            raise ValueError(
                f"unknown ContextSource binding: {source_id.value}"
            ) from exc
        if (
            source_version is not None
            and source_version != binding.contract.source_version
        ):
            raise ValueError("ContextSource version cannot be rebound")
        if (
            contract_fingerprint is not None
            and contract_fingerprint != binding.contract.contract_fingerprint
        ):
            raise ValueError("ContextSource fingerprint cannot be rebound")
        return binding

    def collect(
        self,
        inputs: tuple[ContextSourceCollectInput, ...],
    ) -> tuple[ContextSectionCandidate, ...]:
        ordered_inputs = tuple(
            sorted(
                inputs,
                key=lambda item: (
                    item.authority.source_id.value,
                    item.source_instance_id,
                    item.candidate_key,
                    item.source_revision.source_revision_id,
                ),
            )
        )
        candidates: list[ContextSectionCandidate] = []
        for source_input in ordered_inputs:
            binding = self.resolve(
                source_id=source_input.authority.source_id,
                source_version=source_input.authority.source_contract_version,
                contract_fingerprint=(
                    source_input.authority.source_contract_fingerprint
                ),
            )
            candidates.extend(binding.source.collect(source_input=source_input))
        keys = tuple(
            (
                item.source_id.value,
                item.source_instance_id,
                item.attribution.semantic.candidate_key,
                item.attribution.semantic.source_revision.source_revision_id,
            )
            for item in candidates
        )
        if len(keys) != len(set(keys)):
            raise ValueError("ContextSource candidate semantic keys are duplicated")
        return tuple(candidates)


__all__ = [
    "CanonicalContextSource",
    "ContextSource",
    "ContextSourceBinding",
    "ContextSourceBindingPolicy",
    "ContextSourceRegistry",
    "default_context_source_binding_policy",
]


_SOURCE_POLICY_MATRIX = {
    ContextSourceId.SYSTEM: (
        {"generation_root"},
        {0},
        {True},
        {("system_instruction", "system")},
    ),
    ContextSourceId.RUNTIME_ENVIRONMENT: (
        {"generation_root"},
        {20},
        {False},
        {("leading_context", "user")},
    ),
    ContextSourceId.RUNTIME_CLOCK: (
        {"append_revision"},
        {89},
        {False},
        {("status_observation", "runtime")},
    ),
    ContextSourceId.MEMORY_INSTRUCTION: (
        {"generation_root"},
        {1},
        {False},
        {("system_instruction", "system")},
    ),
    ContextSourceId.MEMORY_PROJECTION: (
        {"append_revision"},
        {40},
        {False},
        {("leading_context", "user")},
    ),
    ContextSourceId.CAPABILITY_CATALOG: (
        {"append_revision"},
        {30},
        {False},
        {("leading_context", "user")},
    ),
    ContextSourceId.ACTIVE_SKILL: (
        {"append_revision"},
        {31},
        {False},
        {("leading_context", "user")},
    ),
    ContextSourceId.WORKSPACE_SKILL: (
        {"append_revision"},
        {32},
        {False},
        {("leading_context", "user")},
    ),
    ContextSourceId.PLAN: (
        {"append_revision", "append_once"},
        {10, 11},
        {True},
        {("leading_context", "user")},
    ),
    ContextSourceId.RECOVERY: (
        {"append_once"},
        {50},
        {False},
        {("leading_context", "user")},
    ),
    ContextSourceId.ROLLOUT_STATUS: (
        {"append_revision"},
        {90},
        {False, True},
        {("status_observation", "runtime")},
    ),
    ContextSourceId.SUBAGENT_HANDOFF: (
        {"append_once"},
        {59},
        {True},
        {("leading_context", "user")},
    ),
    ContextSourceId.SUBAGENT_RESULT: (
        {"append_once"},
        {60},
        {False},
        {("leading_context", "user")},
    ),
    ContextSourceId.MCP_DIAGNOSTIC: (
        {"append_revision"},
        {91},
        {False},
        {("status_observation", "runtime")},
    ),
}
