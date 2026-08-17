"""Immutable, provider-neutral contracts for structured model input.

This module is intentionally pure: it owns no Kernel, transport, repository,
clock, filesystem, or callback capability.  Every value crossing this boundary
is immutable and finite before the compiler is invoked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Literal, Protocol

from pulsara_agent.llm.estimator import TokenEstimate
from pulsara_agent.llm.input import LLMMessage, MessageRole
from pulsara_agent.ports.artifact import (
    ToolOutputArtifactDisposition,
    ToolOutputArtifactUnavailabilityReason,
    ToolResultDisplayKind,
)
from pulsara_agent.ports.tool_execution import (
    ToolOutputSourceCoverage,
    ToolOutputSourceCoverageReason,
)
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    canonical_json_bytes,
    context_fingerprint,
)
from pulsara_agent.primitives.model_call import (
    ResolvedModelCallFact,
    ResolvedModelTargetFact,
    TokenEstimatorFact,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.plan_workflow import (
    PlanApprovedMaterializationDisposition,
    PlanDraftContentIdentity,
    PlanHandoffKind,
    PlanInteractionBinding,
    PlanWorkflowEnteredBy,
    PlanWorkflowStatus,
    extract_plan_draft,
    plan_draft_utf8_digest,
)
from pulsara_agent.primitives.run_permission import (
    FrozenRunPermissionSnapshot,
    RunPermissionOverlay,
)
from pulsara_agent.primitives.tool_observation import (
    FrozenToolObservationTimingFact,
    canonical_utc_timestamp,
)
from pulsara_agent.primitives.tool_result_projection import (
    BEST_AVAILABLE_TOOL_RESULT_DELIVERY,
    FrozenToolResultDeliveryRequirement,
    ToolResultDeliveryRequirement,
    ToolResultFullDeliveryReason,
)


SHA256_PREFIX = "sha256:"


class ModelInputScopeKind(StrEnum):
    ROOT = "ROOT"
    SUBAGENT_TASK = "SUBAGENT_TASK"


class CapabilityActivationSubjectKind(StrEnum):
    ROOT_HUMAN_PROMPT = "ROOT_HUMAN_PROMPT"
    ROOT_NON_HUMAN_TRIGGER = "ROOT_NON_HUMAN_TRIGGER"
    SUBAGENT_OBJECTIVE = "SUBAGENT_OBJECTIVE"


class CanonicalInputOriginKind(StrEnum):
    HUMAN_MESSAGE = "HUMAN_MESSAGE"
    HUMAN_STEER = "HUMAN_STEER"
    SUBAGENT_OBJECTIVE = "SUBAGENT_OBJECTIVE"
    SUBAGENT_RESULT = "SUBAGENT_RESULT"
    JOB_RESULT = "JOB_RESULT"
    PLAN_CONTINUATION = "PLAN_CONTINUATION"


class ContextSourceKind(StrEnum):
    BASE_SYSTEM = "BASE_SYSTEM"
    RUNTIME_ENVIRONMENT = "RUNTIME_ENVIRONMENT"
    RUNTIME_CLOCK = "RUNTIME_CLOCK"
    RUN_PERMISSION = "RUN_PERMISSION"
    PLAN_HANDOFF = "PLAN_HANDOFF"
    PLAN_WORKFLOW = "PLAN_WORKFLOW"
    CAPABILITY_CATALOG = "CAPABILITY_CATALOG"
    MCP_CATALOG = "MCP_CATALOG"
    ACTIVE_SKILL = "ACTIVE_SKILL"
    PREVIOUS_TURN_OUTCOME = "PREVIOUS_TURN_OUTCOME"
    TOOL_OBSERVATION_FRESHNESS = "TOOL_OBSERVATION_FRESHNESS"
    MEMORY_RESPONSE_PREFERENCE_HEAD = "MEMORY_RESPONSE_PREFERENCE_HEAD"
    MEMORY_RECALL = "MEMORY_RECALL"


class ContextChannel(StrEnum):
    SYSTEM = "SYSTEM"
    RUNTIME_OBSERVATION = "RUNTIME_OBSERVATION"


class ContextTrustClass(StrEnum):
    ROOT_INSTRUCTION = "ROOT_INSTRUCTION"
    AUTHORIZED_CAPABILITY_CONTEXT = "AUTHORIZED_CAPABILITY_CONTEXT"
    AUTHORIZED_RUNTIME_GUIDANCE = "AUTHORIZED_RUNTIME_GUIDANCE"
    TRUSTED_RUNTIME_FACT = "TRUSTED_RUNTIME_FACT"
    UNTRUSTED_OBSERVATION = "UNTRUSTED_OBSERVATION"


class ContextSourceLifecycle(StrEnum):
    EPOCH_ROOT = "EPOCH_ROOT"
    SNAPSHOT_ON_CHANGE = "SNAPSHOT_ON_CHANGE"
    CALL_APPEND = "CALL_APPEND"
    TURN_APPEND = "TURN_APPEND"
    TURN_SNAPSHOT = "TURN_SNAPSHOT"
    ACTIVATION_SNAPSHOT = "ACTIVATION_SNAPSHOT"
    ONE_SHOT = "ONE_SHOT"


class ContextSourceAbsenceKind(StrEnum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    EXPLICIT_EMPTY = "EXPLICIT_EMPTY"
    UNAVAILABLE = "UNAVAILABLE"


class ContextBudgetClass(StrEnum):
    MUST_KEEP = "MUST_KEEP"
    IMPORTANT = "IMPORTANT"
    OPTIONAL = "OPTIONAL"
    DEBUG = "DEBUG"


class ContextRenderMode(StrEnum):
    FULL = "FULL"
    COMPACT = "COMPACT"
    SUMMARY = "SUMMARY"
    REF_ONLY = "REF_ONLY"


class ToolResultProviderRenderMode(StrEnum):
    FULL = "FULL"
    COMPACT = "COMPACT"
    REF_ONLY = "REF_ONLY"
    OMITTED_BODY = "OMITTED_BODY"


class ContextPublicDiagnosticCode(StrEnum):
    OPTIONAL_SOURCE_UNAVAILABLE = "OPTIONAL_SOURCE_UNAVAILABLE"
    CAPABILITY_DISCOVERY_INCOMPLETE = "CAPABILITY_DISCOVERY_INCOMPLETE"
    ACTIVE_SKILL_NOT_FOUND = "ACTIVE_SKILL_NOT_FOUND"
    ACTIVE_SKILL_UNAVAILABLE = "ACTIVE_SKILL_UNAVAILABLE"
    CATALOG_TRUNCATED = "CATALOG_TRUNCATED"
    RUNTIME_CLOCK_UNAVAILABLE = "RUNTIME_CLOCK_UNAVAILABLE"
    SOURCE_DEGRADED = "SOURCE_DEGRADED"
    SOURCE_OMITTED = "SOURCE_OMITTED"
    TOOL_RESULT_DEGRADED = "TOOL_RESULT_DEGRADED"
    TOOL_RESULT_BODY_OMITTED = "TOOL_RESULT_BODY_OMITTED"
    SOURCE_VARIANT_NON_PROGRESS = "SOURCE_VARIANT_NON_PROGRESS"
    DECISION_SAMPLE_TRUNCATED = "DECISION_SAMPLE_TRUNCATED"


class ModelInputCompileFailureKind(StrEnum):
    MODEL_TARGET_PREPARATION_FAILED = "MODEL_TARGET_PREPARATION_FAILED"
    TOOL_SURFACE_INVALID = "TOOL_SURFACE_INVALID"
    REQUIRED_SOURCE_UNAVAILABLE = "REQUIRED_SOURCE_UNAVAILABLE"
    SOURCE_CONTRACT_INVALID = "SOURCE_CONTRACT_INVALID"
    SOURCE_PHYSICAL_BOUND_EXCEEDED = "SOURCE_PHYSICAL_BOUND_EXCEEDED"
    COMPILE_WORKING_SET_EXCEEDED = "COMPILE_WORKING_SET_EXCEEDED"
    PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET = "PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET"
    PREFIX_EPOCH_BUDGET_EXHAUSTED = "PREFIX_EPOCH_BUDGET_EXHAUSTED"
    CANONICAL_PREFIX_CONFLICT = "CANONICAL_PREFIX_CONFLICT"
    CANONICAL_DELTA_NOT_PROVIDER_SAFE = "CANONICAL_DELTA_NOT_PROVIDER_SAFE"
    STATEFUL_SOURCE_REPLACEMENT_OVER_BUDGET = (
        "STATEFUL_SOURCE_REPLACEMENT_OVER_BUDGET"
    )
    REQUIRED_CONTEXT_EXCEEDS_BUDGET = "REQUIRED_CONTEXT_EXCEEDS_BUDGET"
    TOOL_SCHEMA_EXCEEDS_BUDGET = "TOOL_SCHEMA_EXCEEDS_BUDGET"
    FINAL_ESTIMATE_MISMATCH = "FINAL_ESTIMATE_MISMATCH"
    FULL_REQUIRED_TOOL_RESULT_NOT_INLINEABLE = (
        "FULL_REQUIRED_TOOL_RESULT_NOT_INLINEABLE"
    )
    FULL_REQUIRED_TOOL_RESULT_EXCEEDS_INPUT_BUDGET = (
        "FULL_REQUIRED_TOOL_RESULT_EXCEEDS_INPUT_BUDGET"
    )
    DEADLINE_EXPIRED = "DEADLINE_EXPIRED"


class StructuredModelInputCompileError(ValueError):
    def __init__(self, kind: ModelInputCompileFailureKind) -> None:
        self.kind = kind
        super().__init__(f"structured model input compile failed: {kind.value}")


MAXIMUM_CANONICAL_PROVIDER_INPUT_ITEMS = 4_096
MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES = 16 << 20


@dataclass(frozen=True, slots=True)
class StructuredModelInputLimits:
    maximum_source_candidates: int = 32
    maximum_variants_per_source: int = 4
    maximum_single_source_variant_bytes: int = 1 << 20
    maximum_aggregate_full_source_bytes: int = 2 << 20
    maximum_aggregate_source_variant_bytes: int = 4 << 20
    maximum_tool_specs: int = 64
    maximum_tool_spec_canonical_bytes: int = 1 << 20
    maximum_compile_working_set_bytes: int = 64 << 20
    maximum_diagnostics: int = 64
    maximum_public_diagnostic_bytes: int = 32 << 10
    maximum_tool_result_compact_bytes: int = 8 << 10
    maximum_tool_result_ref_only_bytes: int = 2 << 10
    maximum_tool_result_decisions: int = 4_096
    maximum_decision_samples: int = 64
    maximum_canonical_input_items: int = MAXIMUM_CANONICAL_PROVIDER_INPUT_ITEMS
    maximum_canonical_input_bytes: int = MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES

    def __post_init__(self) -> None:
        if (
            min(
                self.maximum_source_candidates,
                self.maximum_variants_per_source,
                self.maximum_single_source_variant_bytes,
                self.maximum_aggregate_full_source_bytes,
                self.maximum_aggregate_source_variant_bytes,
                self.maximum_tool_specs,
                self.maximum_tool_spec_canonical_bytes,
                self.maximum_compile_working_set_bytes,
                self.maximum_diagnostics,
                self.maximum_public_diagnostic_bytes,
                self.maximum_tool_result_compact_bytes,
                self.maximum_tool_result_ref_only_bytes,
                self.maximum_tool_result_decisions,
                self.maximum_decision_samples,
                self.maximum_canonical_input_items,
                self.maximum_canonical_input_bytes,
            )
            < 1
        ):
            raise ValueError("structured model input limits must be positive")


STRUCTURED_MODEL_INPUT_LIMITS = StructuredModelInputLimits()


@dataclass(frozen=True, slots=True)
class FrozenToolSpec:
    name: str
    description: str
    parameters: FrozenJsonObjectFact = field(repr=False)
    descriptor_fingerprint: str

    def __post_init__(self) -> None:
        if not self.name or not self.descriptor_fingerprint:
            raise ValueError("frozen tool specification identity is incomplete")
        if not isinstance(self.parameters, FrozenJsonObjectFact):
            raise TypeError("tool parameters must be a frozen JSON object")

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            }
        )


@dataclass(frozen=True, slots=True)
class FrozenModelToolSurface:
    conversation_scope_kind: ModelInputScopeKind
    tool_specs: tuple[FrozenToolSpec, ...] = field(repr=False)
    surface_fingerprint: str

    def __post_init__(self) -> None:
        names = tuple(tool.name for tool in self.tool_specs)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("frozen tool specs must be sorted and unique")
        expected = model_tool_surface_fingerprint(
            self.conversation_scope_kind, self.tool_specs
        )
        if self.surface_fingerprint != expected:
            raise ValueError("tool surface fingerprint mismatch")


def model_tool_surface_fingerprint(
    scope: ModelInputScopeKind, tools: tuple[FrozenToolSpec, ...]
) -> str:
    return context_fingerprint(
        "model-input-tool-surface:v2",
        {
            "scope": scope.value,
            "tools": tuple(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                    "descriptor_fingerprint": tool.descriptor_fingerprint,
                }
                for tool in tools
            ),
        },
    )


class ModelInputTokenEstimator(Protocol):
    fact: TokenEstimatorFact

    def estimate_text(self, text: str) -> int: ...

    def estimate_message(self, message: LLMMessage) -> int: ...

    def estimate_frozen_tool_spec(self, tool: FrozenToolSpec) -> int: ...

    def estimate_frozen_input(
        self,
        *,
        system_prompt: str,
        messages: tuple[LLMMessage, ...],
        tools: tuple[FrozenToolSpec, ...],
    ) -> TokenEstimate: ...


@dataclass(frozen=True, slots=True)
class ModelInputCompileBinding:
    call_fact: ResolvedModelCallFact
    target_fact: ResolvedModelTargetFact
    estimator: ModelInputTokenEstimator = field(repr=False, compare=False)
    estimator_fingerprint: str
    effective_input_budget_tokens: int
    effective_output_tokens: int
    tool_surface: FrozenModelToolSurface = field(repr=False)
    binding_fingerprint: str

    def __post_init__(self) -> None:
        if self.call_fact.target != self.target_fact:
            raise ValueError("compile call and target facts do not join")
        if self.call_fact.purpose.value != "agent_model_loop":
            raise ValueError("compile binding is not an agent model loop")
        if self.estimator.fact.estimator_fingerprint != self.estimator_fingerprint:
            raise ValueError("compile estimator fingerprint mismatch")
        if (
            self.target_fact.token_estimator.estimator_fingerprint
            != self.estimator_fingerprint
        ):
            raise ValueError("compile target estimator fingerprint mismatch")
        if min(self.effective_input_budget_tokens, self.effective_output_tokens) < 1:
            raise ValueError("compile binding budgets must be positive")
        if (
            self.effective_input_budget_tokens
            > self.target_fact.context_budget.input_budget_tokens
        ):
            raise ValueError("compile input budget exceeds the resolved target")
        if (
            self.effective_output_tokens
            != self.target_fact.context_budget.effective_output_tokens
        ):
            raise ValueError("compile output budget differs from the resolved target")
        expected = model_input_compile_binding_fingerprint(
            call_fact=self.call_fact,
            target_fact=self.target_fact,
            estimator_fingerprint=self.estimator_fingerprint,
            effective_input_budget_tokens=self.effective_input_budget_tokens,
            effective_output_tokens=self.effective_output_tokens,
            tool_surface=self.tool_surface,
        )
        if self.binding_fingerprint != expected:
            raise ValueError("compile binding fingerprint mismatch")


def model_input_compile_binding_fingerprint(
    *,
    call_fact: ResolvedModelCallFact,
    target_fact: ResolvedModelTargetFact,
    estimator_fingerprint: str,
    effective_input_budget_tokens: int,
    effective_output_tokens: int,
    tool_surface: FrozenModelToolSurface,
) -> str:
    return context_fingerprint(
        "model-input-compile-binding:v1",
        {
            "call_fact": call_fact,
            "target_fact": target_fact,
            "estimator_fingerprint": estimator_fingerprint,
            "effective_input_budget_tokens": effective_input_budget_tokens,
            "effective_output_tokens": effective_output_tokens,
            "tool_surface_fingerprint": tool_surface.surface_fingerprint,
        },
    )


@dataclass(frozen=True, slots=True)
class ContextRenderVariant:
    mode: ContextRenderMode
    text: str = field(repr=False)
    utf8_bytes: int
    semantic_fingerprint: str

    def __post_init__(self) -> None:
        encoded = self.text.encode("utf-8")
        if len(encoded) != self.utf8_bytes:
            raise ValueError("source variant byte count mismatch")
        expected = context_fingerprint(
            "context-render-variant:v1",
            {"mode": self.mode.value, "text": self.text},
        )
        if self.semantic_fingerprint != expected:
            raise ValueError("source variant fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class ContextSourceCandidate:
    source_kind: ContextSourceKind
    source_instance_id: str
    source_contract_version: str
    source_contract_fingerprint: str
    source_semantic_fingerprint: str
    channel: ContextChannel
    trust_class: ContextTrustClass
    budget_class: ContextBudgetClass
    placement_ordinal: int
    degradation_priority: int
    variants: tuple[ContextRenderVariant, ...] = field(repr=False)
    lifecycle: ContextSourceLifecycle = ContextSourceLifecycle.SNAPSHOT_ON_CHANGE
    domain_semantic_fingerprint: str = ""
    model_visible_memory_fact_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.source_instance_id or not self.source_contract_version:
            raise ValueError("source candidate identity is incomplete")
        if (
            not 0 <= self.placement_ordinal <= 999
            or not 0 <= self.degradation_priority <= 999
        ):
            raise ValueError("source candidate ordinal is outside its closed bound")
        if not self.domain_semantic_fingerprint:
            object.__setattr__(
                self, "domain_semantic_fingerprint", self.source_semantic_fingerprint
            )
        if not self.domain_semantic_fingerprint.startswith(SHA256_PREFIX):
            raise ValueError("source domain semantic fingerprint is invalid")
        if (
            len(self.model_visible_memory_fact_ids) > 128
            or len(set(self.model_visible_memory_fact_ids))
            != len(self.model_visible_memory_fact_ids)
            or any(not value for value in self.model_visible_memory_fact_ids)
            or len(canonical_json_bytes(self.model_visible_memory_fact_ids)) > 16 * 1024
        ):
            raise ValueError("source memory provenance is outside its closed bound")
        if not self.variants:
            raise ValueError("source candidate needs at least one variant")
        modes = tuple(variant.mode for variant in self.variants)
        order = tuple(ContextRenderMode)
        positions = tuple(order.index(mode) for mode in modes)
        if positions != tuple(sorted(set(positions))):
            raise ValueError("source render variants are duplicated or unordered")
        if (
            self.trust_class is ContextTrustClass.ROOT_INSTRUCTION
            and self.channel is not ContextChannel.SYSTEM
        ):
            raise ValueError("root instruction must use SYSTEM channel")
        if (
            self.trust_class is ContextTrustClass.AUTHORIZED_CAPABILITY_CONTEXT
            and self.channel is not ContextChannel.RUNTIME_OBSERVATION
        ):
            raise ValueError("capability context must use runtime observation")
        if (
            self.trust_class is ContextTrustClass.AUTHORIZED_RUNTIME_GUIDANCE
            and self.channel is not ContextChannel.RUNTIME_OBSERVATION
        ):
            raise ValueError("runtime guidance must use runtime observation")
        if (
            self.trust_class is ContextTrustClass.UNTRUSTED_OBSERVATION
            and self.channel is ContextChannel.SYSTEM
        ):
            raise ValueError("untrusted observation cannot use SYSTEM channel")
        expected_contract = context_fingerprint(
            "context-source-contract:v1",
            {
                "kind": self.source_kind.value,
                "version": self.source_contract_version,
                "channel": self.channel.value,
                "trust": self.trust_class.value,
                "budget": self.budget_class.value,
                "placement": self.placement_ordinal,
                "degradation": self.degradation_priority,
                "modes": tuple(mode.value for mode in modes),
                "lifecycle": self.lifecycle.value,
            },
        )
        if self.source_contract_fingerprint != expected_contract:
            raise ValueError("source contract fingerprint mismatch")
        expected_semantic = context_fingerprint(
            "context-source-candidate:v1",
            {
                "source_kind": self.source_kind.value,
                "source_instance_id": self.source_instance_id,
                "source_contract_fingerprint": self.source_contract_fingerprint,
                "variants": tuple(v.semantic_fingerprint for v in self.variants),
            },
        )
        if self.source_semantic_fingerprint != expected_semantic:
            raise ValueError("source candidate semantic fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class ContextSourceCollectionDiagnostic:
    code: ContextPublicDiagnosticCode
    severity: Literal["INFO", "WARNING", "ERROR"]
    source_kind: ContextSourceKind | None

    def __post_init__(self) -> None:
        if self.severity not in {"INFO", "WARNING", "ERROR"}:
            raise ValueError("context diagnostic severity is not closed")


@dataclass(frozen=True, slots=True)
class ContextSourceAbsentFact:
    source_kind: ContextSourceKind
    lifecycle: ContextSourceLifecycle
    absence_kind: ContextSourceAbsenceKind
    source_contract_version: str
    source_contract_fingerprint: str
    trust_class: ContextTrustClass
    budget_class: ContextBudgetClass
    placement_ordinal: int
    degradation_priority: int
    domain_semantic_fingerprint: str

    def __post_init__(self) -> None:
        if not self.source_contract_version:
            raise ValueError("absent source contract is empty")
        for value in (
            self.source_contract_fingerprint,
            self.domain_semantic_fingerprint,
        ):
            if not value.startswith(SHA256_PREFIX):
                raise ValueError("absent source fingerprint is invalid")
        if not 0 <= self.placement_ordinal <= 999:
            raise ValueError("absent source placement is invalid")
        if not 0 <= self.degradation_priority <= 999:
            raise ValueError("absent source degradation priority is invalid")


@dataclass(frozen=True, slots=True)
class CollectedContextSources:
    candidates: tuple[ContextSourceCandidate, ...]
    diagnostics: tuple[ContextSourceCollectionDiagnostic, ...]
    registry_fingerprint: str
    collection_fingerprint: str
    absent_facts: tuple[ContextSourceAbsentFact, ...] = ()

    def __post_init__(self) -> None:
        identities = tuple(item.source_instance_id for item in self.candidates)
        kinds = tuple(item.source_kind for item in self.candidates)
        absent_kinds = tuple(item.source_kind for item in self.absent_facts)
        if (
            len(identities) != len(set(identities))
            or len(kinds) != len(set(kinds))
            or len(absent_kinds) != len(set(absent_kinds))
            or set(kinds).intersection(absent_kinds)
        ):
            raise ValueError("collected source identities or kinds are duplicated")
        expected = context_fingerprint(
            "collected-context-sources:v1",
            {
                "registry_fingerprint": self.registry_fingerprint,
                "candidates": tuple(
                    c.source_semantic_fingerprint for c in self.candidates
                ),
                "diagnostics": tuple(
                    (
                        d.code.value,
                        d.severity,
                        None if d.source_kind is None else d.source_kind.value,
                    )
                    for d in self.diagnostics
                ),
                "absent": tuple(
                    (
                        item.source_kind.value,
                        item.lifecycle.value,
                        item.absence_kind.value,
                        item.domain_semantic_fingerprint,
                    )
                    for item in self.absent_facts
                ),
            },
        )
        if self.collection_fingerprint != expected:
            raise ValueError("source collection fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class ProviderToolCall:
    tool_call_id: str
    tool_name: str
    arguments: FrozenJsonObjectFact = field(repr=False)

    def __post_init__(self) -> None:
        if not self.tool_call_id or not self.tool_name:
            raise ValueError("provider tool call identity is incomplete")
        if not isinstance(self.arguments, FrozenJsonObjectFact):
            raise TypeError("provider tool arguments must be frozen JSON")


@dataclass(frozen=True, slots=True)
class ProviderToolResultContextMetadata:
    result_id: str
    result_state: str
    display_kind: ToolResultDisplayKind
    artifact_disposition: ToolOutputArtifactDisposition
    artifact_id: str | None
    source_coverage: ToolOutputSourceCoverage
    source_coverage_reason: ToolOutputSourceCoverageReason | None
    artifact_unavailability_reason: ToolOutputArtifactUnavailabilityReason | None
    model_visible_memory_fact_ids: tuple[str, ...]
    timing: FrozenToolObservationTimingFact

    def __post_init__(self) -> None:
        if not self.result_id:
            raise ValueError("tool result canonical identity is empty")
        if self.result_state not in {
            "SUCCESS",
            "APPLICATION_ERROR",
            "SYSTEM_ERROR",
            "CANCELLED",
            "INVALID_ARGUMENTS",
            "PERMISSION_DENIED",
            "TOOL_UNAVAILABLE",
            "CANCELLED_BEFORE_DISPATCH",
        }:
            raise ValueError("tool result state is not closed")
        available = self.artifact_disposition in {
            ToolOutputArtifactDisposition.AVAILABLE,
            ToolOutputArtifactDisposition.INCOMPLETE,
        }
        if available != (self.artifact_id is not None):
            raise ValueError("tool result artifact identity is inconsistent")
        if (self.source_coverage is ToolOutputSourceCoverage.COMPLETE) != (
            self.source_coverage_reason is None
        ):
            raise ValueError("tool result source coverage is inconsistent")
        if (self.artifact_disposition is ToolOutputArtifactDisposition.UNAVAILABLE) != (
            self.artifact_unavailability_reason is not None
        ):
            raise ValueError("tool result unavailability is inconsistent")
        if not isinstance(self.timing, FrozenToolObservationTimingFact):
            raise TypeError("tool result timing must be a frozen canonical fact")
        if (
            len(self.model_visible_memory_fact_ids) > 50
            or len(set(self.model_visible_memory_fact_ids))
            != len(self.model_visible_memory_fact_ids)
            or any(not value for value in self.model_visible_memory_fact_ids)
        ):
            raise ValueError("tool result memory provenance header is invalid")


class FrozenProviderInputItemKind(StrEnum):
    CONTEXT_SNAPSHOT = "CONTEXT_SNAPSHOT"
    USER = "USER"
    TERMINAL_OBSERVATION = "TERMINAL_OBSERVATION"
    ASSISTANT = "ASSISTANT"
    ASSISTANT_TOOL_REQUEST = "ASSISTANT_TOOL_REQUEST"
    TOOL_RESULT = "TOOL_RESULT"
    TOOL_RESULT_CLOSURE = "TOOL_RESULT_CLOSURE"
    LATE_TOOL_OUTCOME = "LATE_TOOL_OUTCOME"
    PLAN_CONTINUATION = "PLAN_CONTINUATION"


class ProviderToolResultClosureKind(StrEnum):
    INTERRUPTED_BEFORE_DISPATCH = "interrupted_before_dispatch"
    INTERRUPTED_MAY_HAVE_PARTIALLY_EXECUTED = "interrupted_may_have_partially_executed"
    PLAN_INTERACTION_ABORTED = "plan_interaction_aborted"


@dataclass(frozen=True, slots=True)
class ProviderToolResultClosure:
    assistant_entry_id: str
    tool_call_id: str
    closure_kind: ProviderToolResultClosureKind
    target_provider_input_through_sequence: int


@dataclass(frozen=True, slots=True)
class LateToolOutcomeObservation:
    assistant_entry_id: str
    tool_call_id: str
    result_entry_id: str
    result_entry_sequence: int
    result_state: str


@dataclass(frozen=True, slots=True)
class FrozenProviderInputItem:
    item_kind: FrozenProviderInputItemKind
    source_entry_id: str | None
    source_entry_sequence: int | None
    source_turn_id: str | None
    text: str = field(repr=False)
    input_origin: CanonicalInputOriginKind | None = None
    tool_calls: tuple[ProviderToolCall, ...] = field(default=(), repr=False)
    tool_call_id: str | None = None
    tool_result_context: ProviderToolResultContextMetadata | None = field(
        default=None, repr=False
    )
    tool_result_body_text: str | None = field(default=None, repr=False)
    tool_result_delivery: FrozenToolResultDeliveryRequirement = (
        BEST_AVAILABLE_TOOL_RESULT_DELIVERY
    )

    def __post_init__(self) -> None:
        self.text.encode("utf-8")
        result_kind = self.item_kind in {
            FrozenProviderInputItemKind.TOOL_RESULT,
            FrozenProviderInputItemKind.LATE_TOOL_OUTCOME,
        }
        if result_kind != (
            self.tool_result_context is not None
            and self.tool_result_body_text is not None
        ):
            raise ValueError("tool result context union is invalid")
        if not isinstance(
            self.tool_result_delivery, FrozenToolResultDeliveryRequirement
        ):
            raise TypeError("tool result delivery requirement must be frozen")
        if (
            not result_kind
            and self.tool_result_delivery != BEST_AVAILABLE_TOOL_RESULT_DELIVERY
        ):
            raise ValueError("non-result item cannot require FULL delivery")
        entry_backed = self.item_kind not in {
            FrozenProviderInputItemKind.CONTEXT_SNAPSHOT,
            FrozenProviderInputItemKind.TOOL_RESULT_CLOSURE,
        }
        if entry_backed != (
            self.source_entry_id is not None
            and self.source_entry_sequence is not None
            and self.source_turn_id is not None
        ):
            raise ValueError("provider input entry attribution union is invalid")
        has_origin = self.item_kind in {
            FrozenProviderInputItemKind.USER,
            FrozenProviderInputItemKind.PLAN_CONTINUATION,
        }
        if has_origin != (
            self.input_origin is not None
        ):
            raise ValueError("provider input origin union is invalid")
        if self.source_entry_sequence is not None and self.source_entry_sequence < 0:
            raise ValueError("provider input entry sequence is invalid")
        if self.item_kind is FrozenProviderInputItemKind.CONTEXT_SNAPSHOT and (
            self.source_entry_id is not None
            or self.source_turn_id is not None
            or self.source_entry_sequence is None
        ):
            raise ValueError("context snapshot attribution is invalid")
        call_owner = self.item_kind is (
            FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST
        )
        if call_owner != bool(self.tool_calls):
            raise ValueError("assistant tool-call union is invalid")
        call_ids = tuple(call.tool_call_id for call in self.tool_calls)
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("provider tool-call identities are duplicated")
        call_result_kind = result_kind or self.item_kind is (
            FrozenProviderInputItemKind.TOOL_RESULT_CLOSURE
        )
        if call_result_kind != (self.tool_call_id is not None):
            raise ValueError("provider tool-result call identity union is invalid")
        if (
            self.item_kind is FrozenProviderInputItemKind.TOOL_RESULT
            and self.tool_result_body_text != self.text
        ):
            raise ValueError("ordinary tool result body differs from canonical text")


@dataclass(frozen=True, slots=True)
class CanonicalModelInputIdentity:
    session_id: str
    turn_id: str
    initial_entry_id: str
    context_binding_revision_id: str
    provider_input_through_sequence: int
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    identity_fingerprint: str

    def __post_init__(self) -> None:
        if (
            not self.session_id
            or not self.turn_id
            or not self.initial_entry_id
            or not self.context_binding_revision_id
            or self.provider_input_through_sequence < 0
        ):
            raise ValueError("canonical model input identity is incomplete")
        if (self.conversation_scope_kind is ModelInputScopeKind.ROOT) != (
            self.scope_subagent_task_id is None
        ):
            raise ValueError("canonical model input scope identity is invalid")
        expected = canonical_model_input_identity_fingerprint(
            session_id=self.session_id,
            turn_id=self.turn_id,
            initial_entry_id=self.initial_entry_id,
            context_binding_revision_id=self.context_binding_revision_id,
            provider_input_through_sequence=self.provider_input_through_sequence,
            conversation_scope_kind=self.conversation_scope_kind,
            scope_subagent_task_id=self.scope_subagent_task_id,
        )
        if self.identity_fingerprint != expected:
            raise ValueError("canonical model input identity fingerprint mismatch")


def canonical_model_input_identity_fingerprint(
    *,
    session_id: str,
    turn_id: str,
    initial_entry_id: str,
    context_binding_revision_id: str,
    provider_input_through_sequence: int,
    conversation_scope_kind: ModelInputScopeKind,
    scope_subagent_task_id: str | None,
) -> str:
    return context_fingerprint(
        "canonical-model-input-identity:v1",
        {
            "session_id": session_id,
            "turn_id": turn_id,
            "initial_entry_id": initial_entry_id,
            "context_binding_revision_id": context_binding_revision_id,
            "provider_input_through_sequence": provider_input_through_sequence,
            "conversation_scope_kind": conversation_scope_kind.value,
            "scope_subagent_task_id": scope_subagent_task_id,
        },
    )


@dataclass(frozen=True, slots=True)
class PreparedProviderInputCut:
    session_id: str
    turn_id: str
    context_binding_revision_id: str
    provider_input_through_sequence: int

    def __post_init__(self) -> None:
        if (
            not self.session_id
            or not self.turn_id
            or not self.context_binding_revision_id
            or self.provider_input_through_sequence < 0
        ):
            raise ValueError("prepared provider input cut is incomplete")


class ContextBindingBaseKind(StrEnum):
    FULL_HISTORY = "FULL_HISTORY"
    SNAPSHOT = "SNAPSHOT"


@dataclass(frozen=True, slots=True)
class FrozenContextBindingCompileFact:
    binding_revision_id: str
    revision_ordinal: int
    base_kind: ContextBindingBaseKind
    context_snapshot_id: str | None
    source_through_sequence: int
    context_base_semantic_identity: str
    fact_fingerprint: str

    def __post_init__(self) -> None:
        if (
            not self.binding_revision_id
            or self.revision_ordinal < 0
            or self.source_through_sequence < 0
        ):
            raise ValueError("context binding compile fact is incomplete")
        if (self.base_kind is ContextBindingBaseKind.FULL_HISTORY) != (
            self.context_snapshot_id is None
        ):
            raise ValueError("context binding base union is invalid")
        if not self.context_base_semantic_identity.startswith(SHA256_PREFIX):
            raise ValueError("context base semantic identity is invalid")
        if self.fact_fingerprint != context_binding_compile_fact_fingerprint(self):
            raise ValueError("context binding compile fact fingerprint mismatch")


def context_binding_compile_fact_fingerprint(
    fact: FrozenContextBindingCompileFact,
) -> str:
    return context_fingerprint(
        "pulsara:context-binding-compile-fact:v1",
        {
            "binding_revision_id": fact.binding_revision_id,
            "revision_ordinal": fact.revision_ordinal,
            "base_kind": fact.base_kind.value,
            "context_snapshot_id": fact.context_snapshot_id,
            "source_through_sequence": fact.source_through_sequence,
            "context_base_semantic_identity": fact.context_base_semantic_identity,
        },
    )


@dataclass(frozen=True, slots=True)
class CanonicalModelInputSnapshot:
    identity: CanonicalModelInputIdentity
    items: tuple[FrozenProviderInputItem, ...] = field(repr=False)
    canonical_utf8_bytes: int
    snapshot_fingerprint: str
    closures: tuple[ProviderToolResultClosure, ...] = ()
    late_outcomes: tuple[LateToolOutcomeObservation, ...] = ()

    def __post_init__(self) -> None:
        if self.canonical_utf8_bytes < 0:
            raise ValueError("canonical model input byte count is invalid")
        expected = canonical_model_input_snapshot_fingerprint(
            identity=self.identity,
            items=self.items,
            canonical_utf8_bytes=self.canonical_utf8_bytes,
            closures=self.closures,
            late_outcomes=self.late_outcomes,
        )
        if self.snapshot_fingerprint != expected:
            raise ValueError("canonical model input snapshot fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class FrozenPlanWorkflowCompileFact:
    session_id: str
    workspace_id: str
    turn_id: str
    permission_snapshot_id: str
    permission_snapshot_fingerprint: str
    workflow_id: str
    workflow_ordinal: int
    current_workflow_revision: int
    workflow_status: PlanWorkflowStatus
    entered_by: PlanWorkflowEnteredBy
    resume_permission_mode: PermissionMode
    permission_contract_id: str
    permission_contract_fingerprint: str
    fact_fingerprint: str

    def __post_init__(self) -> None:
        if self.workflow_status is not PlanWorkflowStatus.ACTIVE:
            raise ValueError("compiled Plan workflow must be active")
        if min(self.workflow_ordinal, self.current_workflow_revision) < 1:
            raise ValueError("compiled Plan workflow revision is invalid")
        if self.fact_fingerprint != plan_workflow_compile_fact_fingerprint(self):
            raise ValueError("compiled Plan workflow fingerprint mismatch")


def plan_workflow_compile_fact_fingerprint(
    fact: FrozenPlanWorkflowCompileFact,
) -> str:
    return context_fingerprint(
        "pulsara:plan-workflow-compile-fact:v1",
        {
            "session_id": fact.session_id,
            "workspace_id": fact.workspace_id,
            "turn_id": fact.turn_id,
            "permission_snapshot_id": fact.permission_snapshot_id,
            "permission_snapshot_fingerprint": fact.permission_snapshot_fingerprint,
            "workflow_id": fact.workflow_id,
            "workflow_ordinal": fact.workflow_ordinal,
            "current_workflow_revision": fact.current_workflow_revision,
            "workflow_status": fact.workflow_status.value,
            "entered_by": fact.entered_by.value,
            "resume_permission_mode": fact.resume_permission_mode.value,
            "permission_contract_id": fact.permission_contract_id,
            "permission_contract_fingerprint": fact.permission_contract_fingerprint,
        },
    )


@dataclass(frozen=True, slots=True)
class FrozenPlanHandoffCompileFact:
    session_id: str
    workspace_id: str
    target_turn_id: str
    carrier_entry_id: str
    carrier_entry_sequence: int
    workflow_id: str
    workflow_ordinal: int
    workflow_revision_at_transition: int
    interaction_id: str | None
    handoff_kind: PlanHandoffKind
    workflow_status: PlanWorkflowStatus
    resume_permission_mode: PermissionMode
    transition_semantic_digest: str
    fact_fingerprint: str

    def __post_init__(self) -> None:
        if min(
            self.carrier_entry_sequence,
            self.workflow_ordinal,
            self.workflow_revision_at_transition,
        ) < 1:
            raise ValueError("compiled Plan handoff sequence is invalid")
        if (
            self.handoff_kind is PlanHandoffKind.ENTERED_PLAN
            and self.interaction_id is not None
        ) or (
            self.handoff_kind
            in {
                PlanHandoffKind.REVISION_REQUESTED,
                PlanHandoffKind.APPROVED_PLAN,
            }
            and self.interaction_id is None
        ):
            raise ValueError("compiled Plan handoff interaction is invalid")
        expected_status = {
            PlanHandoffKind.ENTERED_PLAN: PlanWorkflowStatus.ACTIVE,
            PlanHandoffKind.REVISION_REQUESTED: PlanWorkflowStatus.ACTIVE,
            PlanHandoffKind.APPROVED_PLAN: PlanWorkflowStatus.APPROVED,
            PlanHandoffKind.CANCELLED_PLAN: PlanWorkflowStatus.CANCELLED,
            PlanHandoffKind.FORCE_EXITED_PLAN: PlanWorkflowStatus.FORCE_EXITED,
        }[self.handoff_kind]
        if self.workflow_status is not expected_status:
            raise ValueError("compiled Plan handoff status is invalid")
        if self.fact_fingerprint != plan_handoff_compile_fact_fingerprint(self):
            raise ValueError("compiled Plan handoff fingerprint mismatch")


def plan_handoff_compile_fact_fingerprint(
    fact: FrozenPlanHandoffCompileFact,
) -> str:
    return context_fingerprint(
        "pulsara:plan-handoff-compile-fact:v1",
        {
            "session_id": fact.session_id,
            "workspace_id": fact.workspace_id,
            "target_turn_id": fact.target_turn_id,
            "carrier_entry_id": fact.carrier_entry_id,
            "carrier_entry_sequence": fact.carrier_entry_sequence,
            "workflow_id": fact.workflow_id,
            "workflow_ordinal": fact.workflow_ordinal,
            "workflow_revision_at_transition": (
                fact.workflow_revision_at_transition
            ),
            "interaction_id": fact.interaction_id,
            "handoff_kind": fact.handoff_kind.value,
            "workflow_status": fact.workflow_status.value,
            "resume_permission_mode": fact.resume_permission_mode.value,
            "transition_semantic_digest": fact.transition_semantic_digest,
        },
    )


@dataclass(frozen=True, slots=True)
class ApprovedPlanMaterializationFact:
    session_id: str
    workspace_id: str
    target_turn_id: str
    workflow_id: str
    interaction_id: str
    assistant_entry_id: str
    tool_call_id: str
    request_contract_id: str
    request_contract_version: str
    request_contract_fingerprint: str
    request_semantic_digest: str
    content_identity: PlanDraftContentIdentity
    exact_plan_utf8: bytes = field(repr=False)
    disposition: PlanApprovedMaterializationDisposition
    pinned_canonical_item_fingerprint: str | None
    fact_fingerprint: str

    def __post_init__(self) -> None:
        if len(self.exact_plan_utf8) != self.content_identity.plan_utf8_size:
            raise ValueError("approved Plan materialization size mismatch")
        if plan_draft_utf8_digest(self.exact_plan_utf8) != (
            self.content_identity.plan_utf8_digest
        ):
            raise ValueError("approved Plan materialization digest mismatch")
        pinned = self.disposition is (
            PlanApprovedMaterializationDisposition.PIN_EXISTING_CANONICAL_BLOCK
        )
        if pinned != (self.pinned_canonical_item_fingerprint is not None):
            raise ValueError("approved Plan materialization disposition is invalid")
        if self.fact_fingerprint != approved_plan_materialization_fingerprint(self):
            raise ValueError("approved Plan materialization fingerprint mismatch")


def approved_plan_materialization_fingerprint(
    fact: ApprovedPlanMaterializationFact,
) -> str:
    return context_fingerprint(
        "pulsara:approved-plan-materialization-fact:v1",
        {
            "session_id": fact.session_id,
            "workspace_id": fact.workspace_id,
            "target_turn_id": fact.target_turn_id,
            "workflow_id": fact.workflow_id,
            "interaction_id": fact.interaction_id,
            "assistant_entry_id": fact.assistant_entry_id,
            "tool_call_id": fact.tool_call_id,
            "request_contract_id": fact.request_contract_id,
            "request_contract_version": fact.request_contract_version,
            "request_contract_fingerprint": fact.request_contract_fingerprint,
            "request_semantic_digest": fact.request_semantic_digest,
            "content_identity": {
                "interaction_id": fact.content_identity.interaction_id,
                "assistant_entry_id": fact.content_identity.assistant_entry_id,
                "tool_call_id": fact.content_identity.tool_call_id,
                "request_contract_id": fact.content_identity.request_contract_id,
                "request_contract_version": (
                    fact.content_identity.request_contract_version
                ),
                "request_contract_fingerprint": (
                    fact.content_identity.request_contract_fingerprint
                ),
                "request_semantic_digest": (
                    fact.content_identity.request_semantic_digest
                ),
                "plan_utf8_size": fact.content_identity.plan_utf8_size,
                "plan_utf8_digest": fact.content_identity.plan_utf8_digest,
            },
            "disposition": fact.disposition.value,
            "pinned_canonical_item_fingerprint": (
                fact.pinned_canonical_item_fingerprint
            ),
        },
    )


class PreviousTurnOutcomeKind(StrEnum):
    USER_STOPPED = "USER_STOPPED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    HOST_SESSION_CLOSED = "HOST_SESSION_CLOSED"
    HOST_REPLACED = "HOST_REPLACED"
    PROVIDER_INPUT_CONFLICT = "PROVIDER_INPUT_CONFLICT"
    RESOURCE_BOUNDARY = "RESOURCE_BOUNDARY"
    PLAN_CONTINUATION_FAILED = "PLAN_CONTINUATION_FAILED"
    UNKNOWN_INTERRUPTION = "UNKNOWN_INTERRUPTION"


class AcceptedAssistantDisposition(StrEnum):
    NONE_ACCEPTED = "NONE_ACCEPTED"
    ACCEPTED_PREFIX_PRESENT = "ACCEPTED_PREFIX_PRESENT"


@dataclass(frozen=True, slots=True)
class FrozenPreviousTurnOutcomeCompileFact:
    session_id: str
    workspace_id: str
    current_turn_id: str
    current_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    predecessor_turn_id: str
    predecessor_initial_entry_sequence: int
    predecessor_terminal_at_utc: str
    outcome_kind: PreviousTurnOutcomeKind
    accepted_assistant_disposition: AcceptedAssistantDisposition
    accepted_assistant_entry_count: int
    definitely_not_dispatched_tool_count: int
    outcome_unknown_tool_count: int
    bounded_tool_name_samples: tuple[str, ...]
    user_input_preserved: Literal[True]
    canonical_entries_preserved: Literal[True]
    fact_fingerprint: str

    def __post_init__(self) -> None:
        if not all(
            (
                self.session_id,
                self.workspace_id,
                self.current_turn_id,
                self.predecessor_turn_id,
            )
        ):
            raise ValueError("previous-turn outcome identity is incomplete")
        if (self.current_scope_kind is ModelInputScopeKind.ROOT) != (
            self.scope_subagent_task_id is None
        ):
            raise ValueError("previous-turn outcome scope union is invalid")
        if self.predecessor_initial_entry_sequence < 1:
            raise ValueError("previous-turn sequence is invalid")
        parsed = datetime.fromisoformat(
            self.predecessor_terminal_at_utc.replace("Z", "+00:00")
        )
        if canonical_utc_timestamp(parsed) != self.predecessor_terminal_at_utc:
            raise ValueError("previous-turn terminal time is not canonical UTC")
        counts = (
            self.accepted_assistant_entry_count,
            self.definitely_not_dispatched_tool_count,
            self.outcome_unknown_tool_count,
        )
        if min(counts) < 0:
            raise ValueError("previous-turn outcome count is negative")
        expected_disposition = (
            AcceptedAssistantDisposition.ACCEPTED_PREFIX_PRESENT
            if self.accepted_assistant_entry_count
            else AcceptedAssistantDisposition.NONE_ACCEPTED
        )
        if self.accepted_assistant_disposition is not expected_disposition:
            raise ValueError("previous-turn assistant disposition is inconsistent")
        if len(self.bounded_tool_name_samples) > 3:
            raise ValueError("previous-turn tool samples exceed their bound")
        for name in self.bounded_tool_name_samples:
            encoded = name.encode("utf-8")
            if not encoded or len(encoded) > 128:
                raise ValueError("previous-turn tool sample is outside its bound")
        if not self.user_input_preserved or not self.canonical_entries_preserved:
            raise ValueError("previous-turn preservation facts must be true")
        if self.fact_fingerprint != previous_turn_outcome_fingerprint(self):
            raise ValueError("previous-turn outcome fingerprint mismatch")


def previous_turn_outcome_fingerprint(
    fact: FrozenPreviousTurnOutcomeCompileFact,
) -> str:
    return context_fingerprint(
        "pulsara:previous-turn-outcome:v1",
        {
            "session_id": fact.session_id,
            "workspace_id": fact.workspace_id,
            "current_turn_id": fact.current_turn_id,
            "current_scope_kind": fact.current_scope_kind.value,
            "scope_subagent_task_id": fact.scope_subagent_task_id,
            "predecessor_turn_id": fact.predecessor_turn_id,
            "predecessor_initial_entry_sequence": (
                fact.predecessor_initial_entry_sequence
            ),
            "predecessor_terminal_at_utc": fact.predecessor_terminal_at_utc,
            "outcome_kind": fact.outcome_kind.value,
            "accepted_assistant_disposition": (
                fact.accepted_assistant_disposition.value
            ),
            "accepted_assistant_entry_count": fact.accepted_assistant_entry_count,
            "definitely_not_dispatched_tool_count": (
                fact.definitely_not_dispatched_tool_count
            ),
            "outcome_unknown_tool_count": fact.outcome_unknown_tool_count,
            "bounded_tool_name_samples": fact.bounded_tool_name_samples,
            "user_input_preserved": fact.user_input_preserved,
            "canonical_entries_preserved": fact.canonical_entries_preserved,
        },
    )


@dataclass(frozen=True, slots=True)
class FrozenToolObservationFreshnessCompileFact:
    session_id: str
    workspace_id: str
    current_turn_id: str
    current_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    current_turn_ref: str
    current_initial_entry_sequence: int
    immediate_predecessor_turn_id: str | None
    immediate_predecessor_turn_ref: str | None
    classification_contract: Literal[
        "pulsara.tool-observation-freshness.v1"
    ]
    fact_fingerprint: str

    def __post_init__(self) -> None:
        if (self.current_scope_kind is ModelInputScopeKind.ROOT) != (
            self.scope_subagent_task_id is None
        ):
            raise ValueError("tool freshness scope union is invalid")
        if self.current_initial_entry_sequence < 1:
            raise ValueError("tool freshness current sequence is invalid")
        if (self.immediate_predecessor_turn_id is None) != (
            self.immediate_predecessor_turn_ref is None
        ):
            raise ValueError("tool freshness predecessor union is invalid")
        for value in (self.current_turn_ref, self.immediate_predecessor_turn_ref):
            if value is not None and (
                len(value) != 71
                or not value.startswith(SHA256_PREFIX)
                or any(
                    character not in "0123456789abcdef" for character in value[7:]
                )
            ):
                raise ValueError("tool freshness turn reference is invalid")
        if self.classification_contract != "pulsara.tool-observation-freshness.v1":
            raise ValueError("tool freshness classification contract is invalid")
        if self.fact_fingerprint != tool_observation_freshness_fingerprint(self):
            raise ValueError("tool freshness fingerprint mismatch")


def tool_observation_freshness_fingerprint(
    fact: FrozenToolObservationFreshnessCompileFact,
) -> str:
    return context_fingerprint(
        "pulsara:tool-observation-freshness:v1",
        {
            "session_id": fact.session_id,
            "workspace_id": fact.workspace_id,
            "current_turn_id": fact.current_turn_id,
            "current_scope_kind": fact.current_scope_kind.value,
            "scope_subagent_task_id": fact.scope_subagent_task_id,
            "current_turn_ref": fact.current_turn_ref,
            "current_initial_entry_sequence": fact.current_initial_entry_sequence,
            "immediate_predecessor_turn_id": fact.immediate_predecessor_turn_id,
            "immediate_predecessor_turn_ref": fact.immediate_predecessor_turn_ref,
            "classification_contract": fact.classification_contract,
        },
    )


def build_tool_observation_freshness_fact(
    *,
    session_id: str,
    workspace_id: str,
    current_turn_id: str,
    current_scope_kind: ModelInputScopeKind,
    scope_subagent_task_id: str | None,
    current_initial_entry_sequence: int,
    immediate_predecessor_turn_id: str | None,
) -> FrozenToolObservationFreshnessCompileFact:
    values = {
        "session_id": session_id,
        "workspace_id": workspace_id,
        "current_turn_id": current_turn_id,
        "current_scope_kind": current_scope_kind,
        "scope_subagent_task_id": scope_subagent_task_id,
        "current_turn_ref": context_fingerprint(
            "pulsara:provider-visible-turn-ref:v1",
            {"session_id": session_id, "turn_id": current_turn_id},
        ),
        "current_initial_entry_sequence": current_initial_entry_sequence,
        "immediate_predecessor_turn_id": immediate_predecessor_turn_id,
        "immediate_predecessor_turn_ref": (
            None
            if immediate_predecessor_turn_id is None
            else context_fingerprint(
                "pulsara:provider-visible-turn-ref:v1",
                {
                    "session_id": session_id,
                    "turn_id": immediate_predecessor_turn_id,
                },
            )
        ),
        "classification_contract": "pulsara.tool-observation-freshness.v1",
    }
    provisional = FrozenToolObservationFreshnessCompileFact.__new__(
        FrozenToolObservationFreshnessCompileFact
    )
    for name, value in values.items():
        object.__setattr__(provisional, name, value)
    object.__setattr__(provisional, "fact_fingerprint", "")
    return FrozenToolObservationFreshnessCompileFact(
        **values,
        fact_fingerprint=tool_observation_freshness_fingerprint(provisional),
    )


@dataclass(frozen=True, slots=True)
class FrozenCanonicalCompileSnapshot:
    canonical_input: CanonicalModelInputSnapshot = field(repr=False)
    context_binding_fact: FrozenContextBindingCompileFact
    run_permission_snapshot: FrozenRunPermissionSnapshot
    plan_workflow_fact: FrozenPlanWorkflowCompileFact | None
    plan_handoff_fact: FrozenPlanHandoffCompileFact | None
    approved_plan_materialization_fact: ApprovedPlanMaterializationFact | None = field(
        repr=False
    )
    previous_turn_outcome_fact: FrozenPreviousTurnOutcomeCompileFact | None
    tool_observation_freshness_fact: FrozenToolObservationFreshnessCompileFact
    canonical_read_cut_fingerprint: str

    def __post_init__(self) -> None:
        identity = self.canonical_input.identity
        binding = self.context_binding_fact
        if (
            binding.binding_revision_id != identity.context_binding_revision_id
            or binding.source_through_sequence
            > identity.provider_input_through_sequence
        ):
            raise ValueError("compile context binding does not exact-join")
        permission = self.run_permission_snapshot
        if permission.snapshot_id == "":
            raise ValueError("compile permission snapshot is absent")
        workflow = self.plan_workflow_fact
        plan_bound = permission.overlay is RunPermissionOverlay.PLAN_READ_ONLY
        if plan_bound != (workflow is not None):
            raise ValueError("compile Plan workflow presence does not exact-join")
        if workflow is not None and (
            workflow.session_id != identity.session_id
            or workflow.turn_id != identity.turn_id
            or workflow.permission_snapshot_id != permission.snapshot_id
            or workflow.permission_snapshot_fingerprint
            != permission.snapshot_fingerprint
            or workflow.workflow_id != permission.plan_workflow_id
            or workflow.workflow_ordinal
            != permission.plan_context_ordinal_at_admission
            or workflow.current_workflow_revision
            < int(permission.plan_workflow_revision_at_admission or 0)
            or workflow.permission_contract_id
            != permission.permission_contract_id
            or workflow.permission_contract_fingerprint
            != permission.permission_contract_fingerprint
        ):
            raise ValueError("compile Plan workflow does not exact-join")
        handoff = self.plan_handoff_fact
        if handoff is not None:
            expected_item_kind = (
                FrozenProviderInputItemKind.USER
                if handoff.handoff_kind
                in {
                    PlanHandoffKind.CANCELLED_PLAN,
                    PlanHandoffKind.FORCE_EXITED_PLAN,
                }
                else FrozenProviderInputItemKind.PLAN_CONTINUATION
            )
            matching_items = tuple(
                item
                for item in self.canonical_input.items
                if item.source_entry_id == handoff.carrier_entry_id
                and item.source_entry_sequence == handoff.carrier_entry_sequence
                and item.source_turn_id == identity.turn_id
                and item.item_kind is expected_item_kind
            )
            if (
                handoff.session_id != identity.session_id
                or handoff.target_turn_id != identity.turn_id
                or len(matching_items) != 1
                or (
                    workflow is not None
                    and workflow.workspace_id != handoff.workspace_id
                )
            ):
                raise ValueError("compile Plan handoff does not exact-join")
        approved = self.approved_plan_materialization_fact
        if (approved is not None) != (
            handoff is not None
            and handoff.handoff_kind is PlanHandoffKind.APPROVED_PLAN
        ):
            raise ValueError("approved Plan materialization presence is invalid")
        if approved is not None:
            assert handoff is not None
            content = approved.content_identity
            if (
                approved.session_id != identity.session_id
                or approved.workspace_id != handoff.workspace_id
                or approved.target_turn_id != identity.turn_id
                or approved.workflow_id != handoff.workflow_id
                or approved.interaction_id != handoff.interaction_id
                or approved.assistant_entry_id != content.assistant_entry_id
                or approved.tool_call_id != content.tool_call_id
                or approved.interaction_id != content.interaction_id
                or approved.request_contract_id != content.request_contract_id
                or approved.request_contract_version
                != content.request_contract_version
                or approved.request_contract_fingerprint
                != content.request_contract_fingerprint
                or approved.request_semantic_digest
                != content.request_semantic_digest
            ):
                raise ValueError("approved Plan materialization does not exact-join")
            matching_items = tuple(
                item
                for item in self.canonical_input.items
                if item.item_kind
                is FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST
                and item.source_entry_id == approved.assistant_entry_id
                and any(
                    call.tool_call_id == approved.tool_call_id
                    for call in item.tool_calls
                )
            )
            if len(matching_items) > 1:
                raise ValueError("approved Plan canonical carrier is duplicated")
            matching_calls = tuple(
                call
                for item in matching_items
                for call in item.tool_calls
                if call.tool_call_id == approved.tool_call_id
            )
            if matching_calls:
                extracted = extract_plan_draft(
                    interaction_id=approved.interaction_id,
                    assistant_entry_id=approved.assistant_entry_id,
                    tool_call_id=approved.tool_call_id,
                    binding=PlanInteractionBinding(
                        approved.request_contract_id,
                        approved.request_contract_version,
                        approved.request_contract_fingerprint,
                    ),
                    request_semantic_digest=approved.request_semantic_digest,
                    arguments=matching_calls[0].arguments,
                )
                if (
                    extracted.identity != approved.content_identity
                    or extracted.exact_plan_utf8 != approved.exact_plan_utf8
                ):
                    raise ValueError("approved Plan canonical content drifted")
            pinned = approved.disposition is (
                PlanApprovedMaterializationDisposition.PIN_EXISTING_CANONICAL_BLOCK
            )
            if pinned:
                if (
                    len(matching_items) != 1
                    or approved.pinned_canonical_item_fingerprint
                    != provider_input_item_fingerprint(matching_items[0])
                ):
                    raise ValueError("approved Plan pinned carrier does not exact-join")
            elif matching_items:
                raise ValueError("approved Plan materialization would duplicate content")
        previous = self.previous_turn_outcome_fact
        freshness = self.tool_observation_freshness_fact
        if (
            freshness.session_id != identity.session_id
            or freshness.current_turn_id != identity.turn_id
            or freshness.current_scope_kind is not identity.conversation_scope_kind
            or freshness.scope_subagent_task_id != identity.scope_subagent_task_id
        ):
            raise ValueError("tool freshness fact does not exact-join input")
        if previous is not None and (
            previous.session_id != identity.session_id
            or previous.current_turn_id != identity.turn_id
            or previous.current_scope_kind is not identity.conversation_scope_kind
            or previous.scope_subagent_task_id != identity.scope_subagent_task_id
            or previous.predecessor_turn_id
            != freshness.immediate_predecessor_turn_id
        ):
            raise ValueError("previous-turn fact does not exact-join freshness")
        if self.canonical_read_cut_fingerprint != (
            canonical_compile_snapshot_fingerprint(self)
        ):
            raise ValueError("canonical compile snapshot fingerprint mismatch")


def canonical_compile_snapshot_fingerprint(
    snapshot: FrozenCanonicalCompileSnapshot,
) -> str:
    return context_fingerprint(
        "pulsara:canonical-compile-snapshot:v1",
        {
            "canonical_input": snapshot.canonical_input.snapshot_fingerprint,
            "context_binding": snapshot.context_binding_fact.fact_fingerprint,
            "run_permission": (
                snapshot.run_permission_snapshot.snapshot_fingerprint
            ),
            "plan_workflow": (
                None
                if snapshot.plan_workflow_fact is None
                else snapshot.plan_workflow_fact.fact_fingerprint
            ),
            "plan_handoff": (
                None
                if snapshot.plan_handoff_fact is None
                else snapshot.plan_handoff_fact.fact_fingerprint
            ),
            "approved_plan": (
                None
                if snapshot.approved_plan_materialization_fact is None
                else snapshot.approved_plan_materialization_fact.fact_fingerprint
            ),
            "previous_turn_outcome": (
                None
                if snapshot.previous_turn_outcome_fact is None
                else snapshot.previous_turn_outcome_fact.fact_fingerprint
            ),
            "tool_observation_freshness": (
                snapshot.tool_observation_freshness_fact.fact_fingerprint
            ),
        },
    )


def provider_input_item_fingerprint(item: FrozenProviderInputItem) -> str:
    return context_fingerprint(
        "frozen-provider-input-item:v2-tool-result-delivery",
        {
            "kind": item.item_kind.value,
            "entry_id": item.source_entry_id,
            "sequence": item.source_entry_sequence,
            "turn_id": item.source_turn_id,
            "input_origin": (
                None if item.input_origin is None else item.input_origin.value
            ),
            "text": item.text,
            "calls": tuple(
                (call.tool_call_id, call.tool_name, call.arguments)
                for call in item.tool_calls
            ),
            "tool_call_id": item.tool_call_id,
            "tool_result_context": (
                None
                if item.tool_result_context is None
                else {
                    "result_state": item.tool_result_context.result_state,
                    "display_kind": item.tool_result_context.display_kind.value,
                    "artifact_disposition": item.tool_result_context.artifact_disposition.value,
                    "artifact_id": item.tool_result_context.artifact_id,
                    "source_coverage": item.tool_result_context.source_coverage.value,
                    "source_coverage_reason": (
                        None
                        if item.tool_result_context.source_coverage_reason is None
                        else item.tool_result_context.source_coverage_reason.value
                    ),
                    "artifact_unavailability_reason": (
                        None
                        if item.tool_result_context.artifact_unavailability_reason
                        is None
                        else item.tool_result_context.artifact_unavailability_reason.value
                    ),
                    "model_visible_memory_fact_ids": (
                        item.tool_result_context.model_visible_memory_fact_ids
                    ),
                    "timing": item.tool_result_context.timing.fact_fingerprint,
                }
            ),
            "tool_result_body_text": item.tool_result_body_text,
            "tool_result_delivery": {
                "requirement": item.tool_result_delivery.requirement.value,
                "reason": (
                    None
                    if item.tool_result_delivery.reason is None
                    else item.tool_result_delivery.reason.value
                ),
                "classifier_contract": (
                    item.tool_result_delivery.classifier_contract
                ),
            },
        },
    )


def canonical_model_input_snapshot_fingerprint(
    *,
    identity: CanonicalModelInputIdentity,
    items: tuple[FrozenProviderInputItem, ...],
    canonical_utf8_bytes: int,
    closures: tuple[ProviderToolResultClosure, ...],
    late_outcomes: tuple[LateToolOutcomeObservation, ...],
) -> str:
    return context_fingerprint(
        "canonical-model-input-snapshot:v1",
        {
            "identity": identity.identity_fingerprint,
            "items": tuple(provider_input_item_fingerprint(item) for item in items),
            "canonical_utf8_bytes": canonical_utf8_bytes,
            "closures": tuple(
                (
                    item.assistant_entry_id,
                    item.tool_call_id,
                    item.closure_kind.value,
                    item.target_provider_input_through_sequence,
                )
                for item in closures
            ),
            "late_outcomes": tuple(
                (
                    item.assistant_entry_id,
                    item.tool_call_id,
                    item.result_entry_id,
                    item.result_entry_sequence,
                    item.result_state,
                )
                for item in late_outcomes
            ),
        },
    )


@dataclass(frozen=True, slots=True)
class StructuredModelInputCompileRequest:
    context_id: str
    model_call_index: int
    canonical_input: CanonicalModelInputSnapshot = field(repr=False)
    canonical_facts: FrozenCanonicalCompileSnapshot = field(repr=False)
    compile_binding: ModelInputCompileBinding = field(repr=False)
    sources: CollectedContextSources = field(repr=False)
    dispatch_anchor_entry_id: str | None = None
    memory_citation_handles: tuple[tuple[str, str], ...] = field(
        default=(), repr=False
    )

    def __post_init__(self) -> None:
        if not self.context_id or self.model_call_index < 1:
            raise ValueError("compile request identity is invalid")
        identity = self.canonical_input.identity
        if self.canonical_facts.canonical_input.snapshot_fingerprint != (
            self.canonical_input.snapshot_fingerprint
        ):
            raise ValueError("compile request canonical facts do not exact-join")
        if (
            identity.conversation_scope_kind
            is not self.compile_binding.tool_surface.conversation_scope_kind
        ):
            raise ValueError("compile request scope and tool surface differ")
        if self.dispatch_anchor_entry_id is not None and not any(
            item.source_entry_id == self.dispatch_anchor_entry_id
            for item in self.canonical_input.items
        ):
            raise ValueError("compile request dispatch anchor is not canonical")
        result_ids = tuple(item[0] for item in self.memory_citation_handles)
        handles = tuple(item[1] for item in self.memory_citation_handles)
        if (
            len(result_ids) != len(set(result_ids))
            or len(handles) != len(set(handles))
            or any(
                not result_id or not handle
                for result_id, handle in self.memory_citation_handles
            )
        ):
            raise ValueError("compile request memory citation handles are invalid")


@dataclass(frozen=True, slots=True)
class CompiledSourceDecision:
    source_kind: ContextSourceKind
    source_instance_fingerprint: str
    channel: ContextChannel
    selected_mode: ContextRenderMode | None
    included: bool
    estimated_tokens: int
    reason_code: str

    def __post_init__(self) -> None:
        if self.estimated_tokens < 0 or self.reason_code not in {
            "SELECTED_FULL",
            "DEGRADED_FOR_BUDGET",
            "OMITTED_FOR_BUDGET",
        }:
            raise ValueError("compiled source decision is invalid")
        if self.included != (self.selected_mode is not None):
            raise ValueError("compiled source inclusion union is invalid")
        if not self.included and (
            self.estimated_tokens != 0 or self.reason_code != "OMITTED_FOR_BUDGET"
        ):
            raise ValueError("omitted source decision is inconsistent")


@dataclass(frozen=True, slots=True)
class CompiledToolResultDecision:
    source_entry_fingerprint: str
    current_turn: bool
    first_legal_mode: ToolResultProviderRenderMode
    selected_mode: ToolResultProviderRenderMode
    delivery_requirement: ToolResultDeliveryRequirement
    full_delivery_reason: ToolResultFullDeliveryReason | None
    estimated_tokens: int
    reason_code: str

    def __post_init__(self) -> None:
        if self.estimated_tokens < 0 or self.reason_code not in {
            "SELECTED_FULL",
            "FULL_INELIGIBLE_RESULT_BOUND",
            "DEGRADED_FOR_BUDGET",
        }:
            raise ValueError("compiled tool-result decision is invalid")
        expected_reason = (
            "SELECTED_FULL"
            if self.selected_mode is ToolResultProviderRenderMode.FULL
            else "FULL_INELIGIBLE_RESULT_BOUND"
            if self.selected_mode is self.first_legal_mode
            else "DEGRADED_FOR_BUDGET"
        )
        if self.reason_code != expected_reason:
            raise ValueError("compiled tool-result selection reason is inconsistent")
        required = self.delivery_requirement is (
            ToolResultDeliveryRequirement.FULL_REQUIRED
        )
        if required != (self.full_delivery_reason is not None):
            raise ValueError("compiled FULL-delivery requirement is inconsistent")
        if required and self.selected_mode is not ToolResultProviderRenderMode.FULL:
            raise ValueError("required tool result was not compiled as FULL")
        if required and self.first_legal_mode is not ToolResultProviderRenderMode.FULL:
            raise ValueError("required tool result has no legal FULL variant")


@dataclass(frozen=True, slots=True)
class ContextCompileBudgetReport:
    compiler_contract_version: str
    estimator_fingerprint: str
    target_fingerprint: str
    tool_surface_fingerprint: str
    effective_input_budget_tokens: int
    system_tokens: int
    message_tokens: int
    tool_tokens: int
    envelope_tokens: int
    total_input_tokens: int
    protected_transcript_tokens: int
    protected_prefix_message_count: int
    protected_prefix_logical_utf8_bytes: int
    protected_prefix_fingerprint: str | None
    context_source_tokens: int
    degraded_source_count: int
    omitted_source_count: int
    degraded_tool_result_count: int
    omitted_tool_result_body_count: int
    decision_digest: str

    def __post_init__(self) -> None:
        if not self.compiler_contract_version:
            raise ValueError("compile budget report contract is empty")
        numeric = (
            self.effective_input_budget_tokens,
            self.system_tokens,
            self.message_tokens,
            self.tool_tokens,
            self.envelope_tokens,
            self.total_input_tokens,
            self.protected_transcript_tokens,
            self.protected_prefix_message_count,
            self.protected_prefix_logical_utf8_bytes,
            self.context_source_tokens,
            self.degraded_source_count,
            self.omitted_source_count,
            self.degraded_tool_result_count,
            self.omitted_tool_result_body_count,
        )
        if any(value < 0 for value in numeric):
            raise ValueError("compile budget report contains a negative value")
        if self.total_input_tokens != (
            self.system_tokens
            + self.message_tokens
            + self.tool_tokens
            + self.envelope_tokens
        ):
            raise ValueError("compile budget report total is inconsistent")
        if self.total_input_tokens > self.effective_input_budget_tokens:
            raise ValueError("compile budget report exceeds its effective budget")
        for value in (
            self.estimator_fingerprint,
            self.target_fingerprint,
            self.tool_surface_fingerprint,
            self.decision_digest,
        ):
            if not value.startswith(SHA256_PREFIX):
                raise ValueError("compile budget report fingerprint is invalid")
        if self.protected_prefix_fingerprint is not None and not (
            self.protected_prefix_fingerprint.startswith(SHA256_PREFIX)
        ):
            raise ValueError("protected prefix fingerprint is invalid")
        if (self.protected_prefix_message_count == 0) != (
            self.protected_prefix_fingerprint is None
        ):
            raise ValueError("protected prefix summary union is invalid")


@dataclass(frozen=True, slots=True)
class FrozenCompiledMessagePlacement:
    message_ordinal: int
    origin_entry_id: str | None
    origin_item_fingerprint: str
    within_origin_ordinal: int
    role: MessageRole
    placement_fingerprint: str

    def __post_init__(self) -> None:
        if self.message_ordinal < 0 or self.within_origin_ordinal < 0:
            raise ValueError("compiled message placement ordinal is invalid")
        if not self.origin_item_fingerprint.startswith(SHA256_PREFIX):
            raise ValueError("compiled message origin fingerprint is invalid")
        expected = context_fingerprint(
            "pulsara.compiled-message-placement:v1",
            {
                "ordinal": self.message_ordinal,
                "entry": self.origin_entry_id,
                "item": self.origin_item_fingerprint,
                "within": self.within_origin_ordinal,
                "role": self.role.value,
            },
        )
        if self.placement_fingerprint != expected:
            raise ValueError("compiled message placement fingerprint mismatch")


def compiled_message_placements_fingerprint(
    placements: tuple[FrozenCompiledMessagePlacement, ...],
) -> str:
    return context_fingerprint(
        "pulsara.compiled-message-placements:v1",
        tuple(item.placement_fingerprint for item in placements),
    )


@dataclass(frozen=True, slots=True)
class FrozenCompiledModelInput:
    context_id: str
    canonical_input_identity: CanonicalModelInputIdentity
    system_prompt: str = field(repr=False)
    messages: tuple[LLMMessage, ...] = field(repr=False)
    message_placements: tuple[FrozenCompiledMessagePlacement, ...] = field(
        repr=False
    )
    message_placements_fingerprint: str
    tools: tuple[FrozenToolSpec, ...] = field(repr=False)
    final_estimate: TokenEstimate
    source_decisions: tuple[CompiledSourceDecision, ...]
    tool_result_decisions: tuple[CompiledToolResultDecision, ...]
    budget_report: ContextCompileBudgetReport
    diagnostic_codes: tuple[ContextPublicDiagnosticCode, ...]
    source_collection_fingerprint: str
    compiled_semantic_fingerprint: str
    compile_binding_fingerprint: str

    def __post_init__(self) -> None:
        if not self.context_id:
            raise ValueError("compiled model input context identity is empty")
        if (
            self.final_estimate.total_input_tokens
            != self.budget_report.total_input_tokens
        ):
            raise ValueError("compiled model input estimate and report differ")
        if (
            self.final_estimate.system_tokens != self.budget_report.system_tokens
            or self.final_estimate.message_tokens != self.budget_report.message_tokens
            or self.final_estimate.tool_tokens != self.budget_report.tool_tokens
            or self.final_estimate.envelope_tokens != self.budget_report.envelope_tokens
        ):
            raise ValueError("compiled model input component estimates differ")
        if (
            self.final_estimate.total_input_tokens
            > self.budget_report.effective_input_budget_tokens
        ):
            raise ValueError("compiled model input exceeds its effective budget")
        if len(self.message_placements) != len(self.messages):
            raise ValueError("compiled message placements are not parallel")
        if tuple(item.message_ordinal for item in self.message_placements) != tuple(
            range(len(self.messages))
        ):
            raise ValueError("compiled message placement order is invalid")
        if any(
            item.role is not message.role
            for item, message in zip(
                self.message_placements, self.messages, strict=True
            )
        ):
            raise ValueError("compiled message placement role drifted")
        if self.message_placements_fingerprint != (
            compiled_message_placements_fingerprint(self.message_placements)
        ):
            raise ValueError("compiled message placements fingerprint mismatch")
        if (
            len(self.tool_result_decisions)
            > STRUCTURED_MODEL_INPUT_LIMITS.maximum_tool_result_decisions
        ):
            raise ValueError("compiled tool-result decisions exceed the hard bound")
        if (
            len(self.diagnostic_codes)
            > STRUCTURED_MODEL_INPUT_LIMITS.maximum_diagnostics
        ):
            raise ValueError("compiled diagnostics exceed the hard bound")
        if len(self.diagnostic_codes) != len(set(self.diagnostic_codes)):
            raise ValueError("compiled diagnostics are duplicated")
        for value in (
            self.source_collection_fingerprint,
            self.compiled_semantic_fingerprint,
            self.compile_binding_fingerprint,
        ):
            if not value.startswith(SHA256_PREFIX):
                raise ValueError("compiled model input fingerprint is invalid")
        expected = frozen_compiled_model_input_fingerprint(
            context_id=self.context_id,
            canonical_input_identity=self.canonical_input_identity,
            system_prompt=self.system_prompt,
            messages=self.messages,
            tools=self.tools,
            final_estimate=self.final_estimate,
            source_decisions=self.source_decisions,
            tool_result_decisions=self.tool_result_decisions,
            budget_report=self.budget_report,
            diagnostic_codes=self.diagnostic_codes,
            source_collection_fingerprint=self.source_collection_fingerprint,
            compile_binding_fingerprint=self.compile_binding_fingerprint,
        )
        if self.compiled_semantic_fingerprint != expected:
            raise ValueError("compiled model input fingerprint mismatch")


def frozen_compiled_model_input_fingerprint(
    *,
    context_id: str,
    canonical_input_identity: CanonicalModelInputIdentity,
    system_prompt: str,
    messages: tuple[LLMMessage, ...],
    tools: tuple[FrozenToolSpec, ...],
    final_estimate: TokenEstimate,
    source_decisions: tuple[CompiledSourceDecision, ...],
    tool_result_decisions: tuple[CompiledToolResultDecision, ...],
    budget_report: ContextCompileBudgetReport,
    diagnostic_codes: tuple[ContextPublicDiagnosticCode, ...],
    source_collection_fingerprint: str,
    compile_binding_fingerprint: str,
) -> str:
    return context_fingerprint(
        "frozen-compiled-model-input:v1",
        {
            "context_id": context_id,
            "canonical_identity": canonical_input_identity.identity_fingerprint,
            "compile_binding": compile_binding_fingerprint,
            "source_collection": source_collection_fingerprint,
            "system_prompt": system_prompt,
            "messages": tuple(_llm_message_value(item) for item in messages),
            "tools": tuple(tool.canonical_bytes.decode("utf-8") for tool in tools),
            "estimate": _token_estimate_value(final_estimate),
            "source_decisions": tuple(
                (
                    item.source_kind.value,
                    item.source_instance_fingerprint,
                    item.channel.value,
                    None if item.selected_mode is None else item.selected_mode.value,
                    item.included,
                    item.estimated_tokens,
                    item.reason_code,
                )
                for item in source_decisions
            ),
            "tool_result_decisions": tuple(
                (
                    item.source_entry_fingerprint,
                    item.current_turn,
                    item.first_legal_mode.value,
                    item.selected_mode.value,
                    item.delivery_requirement.value,
                    (
                        None
                        if item.full_delivery_reason is None
                        else item.full_delivery_reason.value
                    ),
                    item.estimated_tokens,
                    item.reason_code,
                )
                for item in tool_result_decisions
            ),
            "budget_report": _budget_report_value(budget_report),
            "diagnostics": tuple(item.value for item in diagnostic_codes),
        },
    )


def _llm_message_value(message: LLMMessage) -> dict[str, object]:
    return {
        "role": message.role.value,
        "content": message.content,
        "thinking": message.thinking,
        "tool_calls": tuple(
            (call.id, call.name, call.arguments) for call in message.tool_calls
        ),
        "tool_call_id": message.tool_call_id,
        "name": message.name,
        "arguments": message.arguments,
    }


def _token_estimate_value(estimate: TokenEstimate) -> dict[str, object]:
    return {
        "system_tokens": estimate.system_tokens,
        "message_tokens": estimate.message_tokens,
        "message_tokens_by_index": estimate.message_tokens_by_index,
        "tool_tokens": estimate.tool_tokens,
        "envelope_tokens": estimate.envelope_tokens,
        "total_input_tokens": estimate.total_input_tokens,
    }


def _budget_report_value(report: ContextCompileBudgetReport) -> dict[str, object]:
    return {
        "compiler_contract_version": report.compiler_contract_version,
        "estimator_fingerprint": report.estimator_fingerprint,
        "target_fingerprint": report.target_fingerprint,
        "tool_surface_fingerprint": report.tool_surface_fingerprint,
        "effective_input_budget_tokens": report.effective_input_budget_tokens,
        "system_tokens": report.system_tokens,
        "message_tokens": report.message_tokens,
        "tool_tokens": report.tool_tokens,
        "envelope_tokens": report.envelope_tokens,
        "total_input_tokens": report.total_input_tokens,
        "protected_transcript_tokens": report.protected_transcript_tokens,
        "protected_prefix_message_count": report.protected_prefix_message_count,
        "protected_prefix_logical_utf8_bytes": (
            report.protected_prefix_logical_utf8_bytes
        ),
        "protected_prefix_fingerprint": report.protected_prefix_fingerprint,
        "context_source_tokens": report.context_source_tokens,
        "degraded_source_count": report.degraded_source_count,
        "omitted_source_count": report.omitted_source_count,
        "degraded_tool_result_count": report.degraded_tool_result_count,
        "omitted_tool_result_body_count": report.omitted_tool_result_body_count,
        "decision_digest": report.decision_digest,
    }


@dataclass(frozen=True, slots=True)
class RuntimeTemporalCapture:
    observed_at_utc: datetime
    local_date: date
    timezone_name: str
    utc_offset_minutes: int
    capture_fingerprint: str

    def __post_init__(self) -> None:
        if (
            self.observed_at_utc.tzinfo is None
            or self.observed_at_utc.utcoffset() is None
            or self.observed_at_utc.utcoffset().total_seconds() != 0
            or not self.timezone_name
            or not -1_440 < self.utc_offset_minutes < 1_440
        ):
            raise ValueError("runtime temporal capture is invalid")
        expected = context_fingerprint(
            "runtime-temporal-capture:v1",
            {
                "observed_at_utc": self.observed_at_utc.isoformat(),
                "local_date": self.local_date.isoformat(),
                "timezone_name": self.timezone_name,
                "utc_offset_minutes": self.utc_offset_minutes,
            },
        )
        if self.capture_fingerprint != expected:
            raise ValueError("runtime temporal capture fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class RuntimeEnvironmentSnapshot:
    workspace_kind: Literal["project", "transient"]
    workspace_root: str
    terminal_current_cwd: str
    timezone_name: str
    utc_offset_minutes: int | None
    snapshot_fingerprint: str

    def __post_init__(self) -> None:
        if (
            self.workspace_kind not in {"project", "transient"}
            or not self.workspace_root
            or not self.terminal_current_cwd
            or not self.timezone_name
            or (
                self.utc_offset_minutes is not None
                and not -1_440 < self.utc_offset_minutes < 1_440
            )
        ):
            raise ValueError("runtime environment snapshot is invalid")
        expected = context_fingerprint(
            "runtime-environment-snapshot:v1",
            {
                "workspace_kind": self.workspace_kind,
                "workspace_root": self.workspace_root,
                "terminal_current_cwd": self.terminal_current_cwd,
                "timezone_name": self.timezone_name,
                "utc_offset_minutes": self.utc_offset_minutes,
            },
        )
        if self.snapshot_fingerprint != expected:
            raise ValueError("runtime environment snapshot fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class RuntimeClockSnapshot:
    observed_at_utc: datetime
    local_date: date
    timezone_name: str
    utc_offset_minutes: int
    temporal_capture_fingerprint: str

    def __post_init__(self) -> None:
        if (
            self.observed_at_utc.tzinfo is None
            or self.observed_at_utc.utcoffset() is None
            or self.observed_at_utc.utcoffset().total_seconds() != 0
            or not self.timezone_name
            or not -1_440 < self.utc_offset_minutes < 1_440
        ):
            raise ValueError("runtime clock snapshot is invalid")
        expected = context_fingerprint(
            "runtime-temporal-capture:v1",
            {
                "observed_at_utc": self.observed_at_utc.isoformat(),
                "local_date": self.local_date.isoformat(),
                "timezone_name": self.timezone_name,
                "utc_offset_minutes": self.utc_offset_minutes,
            },
        )
        if self.temporal_capture_fingerprint != expected:
            raise ValueError("runtime clock temporal capture does not exact-join")


__all__ = [name for name in globals() if not name.startswith("_")]
