"""Immutable contracts for long-horizon context windows.

This module intentionally contains no runtime services.  Facts defined here
may be persisted in events, manifests, and checkpoint artifacts.
"""

from __future__ import annotations

from enum import StrEnum
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pulsara_agent.primitives._context_base import (
    ContextEventReferenceFact,
    context_fingerprint,
)
from pulsara_agent.primitives.model_call import (
    ModelCallPurpose,
    ResolvedModelCallFact,
    ResolvedModelTargetFact,
)
from pulsara_agent.primitives.subagent import ChildNativeTerminalReferenceFact


class FrozenLongHorizonFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ContextWindowOpenReason(StrEnum):
    INITIAL_RUN = "initial_run"
    LLM_COMPACTION = "llm_compaction"


class ContextWindowCloseReason(StrEnum):
    LLM_COMPACTION = "llm_compaction"
    RUN_FINISHED = "run_finished"
    RUN_FAILED = "run_failed"
    USER_STOP = "user_stop"
    HOST_TEARDOWN = "host_teardown"
    RECOVERED_INTERRUPTED = "recovered_interrupted"


class ToolObservationRepresentation(StrEnum):
    FULL = "full"
    PREVIEW = "preview"
    ESSENTIAL = "essential"
    ARTIFACT_LOCATOR = "artifact_locator"
    ROLLUP_MEMBER = "rollup_member"
    PAIR_STUB = "pair_stub"


TOOL_OBSERVATION_REPRESENTATION_RANK: dict[ToolObservationRepresentation, int] = {
    ToolObservationRepresentation.FULL: 60,
    ToolObservationRepresentation.PREVIEW: 50,
    ToolObservationRepresentation.ESSENTIAL: 40,
    ToolObservationRepresentation.ARTIFACT_LOCATOR: 30,
    ToolObservationRepresentation.ROLLUP_MEMBER: 20,
    ToolObservationRepresentation.PAIR_STUB: 10,
}


class ProjectionRewriteReason(StrEnum):
    NEW_RESULT_INGESTED = "new_result_ingested"
    SOFT_TARGET_EXCEEDED = "soft_target_exceeded"
    HARD_AVAILABLE_PRESSURE = "hard_available_pressure"
    OLD_COMPLETED_BODY = "old_completed_body"
    REPEATED_OBSERVATION = "repeated_observation"
    ROLLUP_CREATED = "rollup_created"
    FINALIZATION_NARROWING = "finalization_narrowing"
    WINDOW_OPEN_NORMALIZATION = "window_open_normalization"


class LongHorizonPreparationStage(StrEnum):
    CHECKPOINT_RESTORE = "checkpoint_restore"
    STATE_REBUILD = "state_rebuild"
    SETTLEMENT = "settlement"
    PROJECTION_PLANNING = "projection_planning"
    PROJECTION_COMMIT = "projection_commit"
    WINDOW_COMPACTION = "window_compaction"
    ROLLOUT_ADMISSION = "rollout_admission"
    CONTEXT_INPUT = "context_input"
    CONTEXT_COMPILE = "context_compile"
    PRE_SEND_VALIDATION = "pre_send_validation"


class RolloutPhase(StrEnum):
    EXPLORATION = "exploration"
    WARNING = "warning"
    RESTRICTED = "restricted"
    FINALIZATION_ONLY = "finalization_only"
    EXHAUSTED = "exhausted"
    EMERGENCY_HARD_STOP = "emergency_hard_stop"


ROLLOUT_PHASE_ORDER: tuple[RolloutPhase, ...] = tuple(RolloutPhase)


class RolloutBudgetBucket(StrEnum):
    EXPLORATION = "exploration"
    FINALIZATION_AGENT = "finalization_agent"
    FINALIZATION_COMPACTION = "finalization_compaction"
    FINALIZATION_TOOL = "finalization_tool"


class RolloutTransitionReason(StrEnum):
    WEIGHTED_TOKEN_THRESHOLD = "weighted_token_threshold"
    TOOL_COST_THRESHOLD = "tool_cost_threshold"
    MODEL_CALL_THRESHOLD = "model_call_threshold"
    EXPLORATION_ADMISSION_UNREACHABLE = "exploration_admission_unreachable"
    EXPLORATION_COMPACTION_ADMISSION_UNREACHABLE = (
        "exploration_compaction_admission_unreachable"
    )
    WINDOW_COMPACTION_UNAVAILABLE = "window_compaction_unavailable"
    FINALIZATION_AGENT_UNAVAILABLE = "finalization_agent_unavailable"
    EMERGENCY_CIRCUIT_BREAKER = "emergency_circuit_breaker"


class LongHorizonActionClass(StrEnum):
    EVIDENCE_ACQUISITION = "evidence_acquisition"
    EVIDENCE_HYDRATION = "evidence_hydration"
    SYNTHESIS_MUTATION = "synthesis_mutation"
    BOUNDED_VERIFICATION = "bounded_verification"
    USER_INTERACTION = "user_interaction"
    PROCESS_CONTROL = "process_control"
    EXTERNAL_ACTION = "external_action"


class ToolActionClassifierContractFact(FrozenLongHorizonFact):
    schema_version: Literal["tool_action_classifier_contract.v1"] = (
        "tool_action_classifier_contract.v1"
    )
    classifier_id: str = Field(min_length=1)
    classifier_version: str = Field(min_length=1)
    input_schema_fingerprint: str = Field(min_length=1)
    output_schema_fingerprint: str = Field(min_length=1)
    classification_policy_fingerprint: str = Field(min_length=1)
    contract_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _contract(self) -> "ToolActionClassifierContractFact":
        _validate_fingerprint(
            self,
            namespace="tool-action-classifier-contract:v1",
            field_name="contract_fingerprint",
        )
        return self


class LongHorizonToolPolicyFact(FrozenLongHorizonFact):
    schema_version: Literal["long_horizon_tool_policy.v1"] = (
        "long_horizon_tool_policy.v1"
    )
    allowed_action_classes: tuple[LongHorizonActionClass, ...]
    max_rollout_cost_units: int = Field(ge=0)
    allowed_in_phases: tuple[RolloutPhase, ...]
    action_classifier_contract: ToolActionClassifierContractFact
    policy_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _policy(self) -> "LongHorizonToolPolicyFact":
        if not self.allowed_action_classes:
            raise ValueError("long-horizon tool policy requires an action class")
        if len(self.allowed_action_classes) != len(set(self.allowed_action_classes)):
            raise ValueError("long-horizon tool action classes must be unique")
        if not self.allowed_in_phases:
            raise ValueError("long-horizon tool policy requires an allowed phase")
        if len(self.allowed_in_phases) != len(set(self.allowed_in_phases)):
            raise ValueError("long-horizon tool phases must be unique")
        _validate_fingerprint(
            self,
            namespace="long-horizon-tool-policy:v1",
            field_name="policy_fingerprint",
        )
        return self


class ToolActionClassificationFact(FrozenLongHorizonFact):
    schema_version: Literal["tool_action_classification.v1"] = (
        "tool_action_classification.v1"
    )
    tool_call_id: str = Field(min_length=1)
    descriptor_id: str = Field(min_length=1)
    descriptor_fingerprint: str = Field(min_length=1)
    action_class: LongHorizonActionClass
    rollout_cost_units: int = Field(ge=0)
    normalized_action_fingerprint: str = Field(min_length=1)
    classifier_id: str = Field(min_length=1)
    classifier_version: str = Field(min_length=1)
    classifier_contract_fingerprint: str = Field(min_length=1)
    classification_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _classification(self) -> "ToolActionClassificationFact":
        _validate_fingerprint(
            self,
            namespace="tool-action-classification:v1",
            field_name="classification_fingerprint",
        )
        return self


class LongHorizonDiagnosticFact(FrozenLongHorizonFact):
    code: str = Field(min_length=1, max_length=96)
    message: str = Field(default="", max_length=512)
    stage: LongHorizonPreparationStage | None = None
    attributes: tuple[tuple[str, str | int | float | bool | None], ...] = ()

    @model_validator(mode="after")
    def _bounded_attributes(self) -> "LongHorizonDiagnosticFact":
        if len(self.attributes) > 16:
            raise ValueError("long-horizon diagnostic attributes exceed 16 entries")
        keys = tuple(key for key, _value in self.attributes)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("diagnostic attributes must have sorted unique keys")
        for key, value in self.attributes:
            if not key or len(key) > 96:
                raise ValueError("diagnostic attribute key is invalid")
            if isinstance(value, str) and len(value) > 256:
                raise ValueError("diagnostic string attribute exceeds 256 characters")
        return self


class EventSchemaDomainContractFact(FrozenLongHorizonFact):
    event_type: str = Field(min_length=1)
    event_schema_version: str = Field(min_length=1)
    event_schema_fingerprint: str = Field(min_length=1)
    event_domain: Literal["subagent_graph", "non_graph"]
    decoder_contract_fingerprint: str = Field(min_length=1)
    domain_contract_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "EventSchemaDomainContractFact":
        expected = context_fingerprint(
            "event-schema-domain-contract:v1",
            self.model_dump(mode="json", exclude={"domain_contract_fingerprint"}),
        )
        if self.domain_contract_fingerprint != expected:
            raise ValueError("event schema domain contract fingerprint mismatch")
        return self


class SupportedGraphEventContractFact(FrozenLongHorizonFact):
    event_type: str = Field(min_length=1)
    event_schema_version: str = Field(min_length=1)
    event_schema_fingerprint: str = Field(min_length=1)
    event_domain_contract_fingerprint: str = Field(min_length=1)
    semantic_projection_contract_fingerprint: str = Field(min_length=1)
    supported_event_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "SupportedGraphEventContractFact":
        expected = context_fingerprint(
            "supported-subagent-graph-event:v1",
            self.model_dump(mode="json", exclude={"supported_event_fingerprint"}),
        )
        if self.supported_event_fingerprint != expected:
            raise ValueError("supported graph event fingerprint mismatch")
        return self


class SubagentGraphReducerContractFact(FrozenLongHorizonFact):
    schema_version: Literal["subagent_graph_reducer_contract.v1"] = (
        "subagent_graph_reducer_contract.v1"
    )
    graph_reducer_id: str = Field(min_length=1)
    graph_reducer_version: str = Field(min_length=1)
    graph_schema_version: str = Field(min_length=1)
    supported_graph_events: tuple[SupportedGraphEventContractFact, ...]
    event_filter_contract_fingerprint: str = Field(min_length=1)
    graph_semantic_event_canonicalization_fingerprint: str = Field(min_length=1)
    transition_contract_fingerprint: str = Field(min_length=1)
    invariant_contract_fingerprint: str = Field(min_length=1)
    canonical_state_contract_fingerprint: str = Field(min_length=1)
    graph_reducer_contract_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _contract(self) -> "SubagentGraphReducerContractFact":
        identities = tuple(
            (item.event_type, item.event_schema_version)
            for item in self.supported_graph_events
        )
        if identities != tuple(sorted(identities)) or len(identities) != len(
            set(identities)
        ):
            raise ValueError("supported graph event contracts must be sorted and unique")
        expected = context_fingerprint(
            "subagent-graph-reducer-contract:v1",
            self.model_dump(
                mode="json", exclude={"graph_reducer_contract_fingerprint"}
            ),
        )
        if self.graph_reducer_contract_fingerprint != expected:
            raise ValueError("subagent graph reducer contract fingerprint mismatch")
        return self


class SubagentGraphSemanticSourceFact(FrozenLongHorizonFact):
    schema_version: Literal["subagent_graph_semantic_source.v1"] = (
        "subagent_graph_semantic_source.v1"
    )
    runtime_session_id: str = Field(min_length=1)
    graph_event_count: int = Field(ge=0)
    graph_semantic_accumulator: str = Field(min_length=1)
    graph_reducer_id: str = Field(min_length=1)
    graph_reducer_version: str = Field(min_length=1)
    graph_reducer_contract_fingerprint: str = Field(min_length=1)
    graph_state_semantic_fingerprint: str = Field(min_length=1)
    semantic_source_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "SubagentGraphSemanticSourceFact":
        expected = context_fingerprint(
            "subagent-graph-semantic-source:v1",
            self.model_dump(mode="json", exclude={"semantic_source_fingerprint"}),
        )
        if self.semantic_source_fingerprint != expected:
            raise ValueError("subagent graph semantic source fingerprint mismatch")
        return self


class SubagentGraphAccelerationFact(FrozenLongHorizonFact):
    schema_version: Literal["subagent_graph_acceleration.v1"] = (
        "subagent_graph_acceleration.v1"
    )
    checkpoint_id: str = Field(min_length=1)
    checkpoint_materialization_event_id: str = Field(min_length=1)
    checkpoint_through_sequence: int = Field(ge=1)
    checkpoint_ledger_continuity_accumulator: str = Field(min_length=1)
    delta_from_sequence: int = Field(ge=1)
    delta_through_sequence: int = Field(ge=0)
    delta_count: int = Field(ge=0)
    delta_byte_count: int = Field(ge=0)
    ledger_through_sequence: int = Field(ge=1)
    ledger_continuity_accumulator: str = Field(min_length=1)
    acceleration_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _invariants(self) -> "SubagentGraphAccelerationFact":
        if self.checkpoint_through_sequence > self.ledger_through_sequence:
            raise ValueError("checkpoint is newer than acceleration ledger high-water")
        if self.delta_from_sequence != self.checkpoint_through_sequence + 1:
            raise ValueError("checkpoint delta must start after checkpoint high-water")
        if self.delta_through_sequence != self.ledger_through_sequence:
            raise ValueError("checkpoint delta must end at ledger high-water")
        expected_count = max(
            0, self.delta_through_sequence - self.delta_from_sequence + 1
        )
        if self.delta_count != expected_count:
            raise ValueError("checkpoint delta count does not match its range")
        expected = context_fingerprint(
            "subagent-graph-acceleration:v1",
            self.model_dump(mode="json", exclude={"acceleration_fingerprint"}),
        )
        if self.acceleration_fingerprint != expected:
            raise ValueError("subagent graph acceleration fingerprint mismatch")
        return self


class SubagentGraphCheckpointStateFact(FrozenLongHorizonFact):
    schema_version: Literal["subagent_graph_checkpoint.v1"] = (
        "subagent_graph_checkpoint.v1"
    )
    parent_runtime_session_id: str = Field(min_length=1)
    checkpoint_id: str = Field(min_length=1)
    through_sequence: int = Field(ge=1)
    graph_reducer_id: str = Field(min_length=1)
    graph_reducer_version: str = Field(min_length=1)
    graph_reducer_contract_fingerprint: str = Field(min_length=1)
    graph_schema_version: str = Field(min_length=1)
    graph_state_semantic_fingerprint: str = Field(min_length=1)
    graph_event_count: int = Field(ge=0)
    graph_semantic_accumulator: str = Field(min_length=1)
    ledger_continuity_accumulator: str = Field(min_length=1)
    run_count: int = Field(ge=0)
    task_count: int = Field(ge=0)
    result_count: int = Field(ge=0)
    edge_count: int = Field(ge=0)
    delivery_count: int = Field(ge=0)
    consistent: Literal[True] = True


class SubagentGraphCheckpointArtifactFact(FrozenLongHorizonFact):
    artifact_id: str = Field(min_length=1)
    media_type: Literal[
        "application/vnd.pulsara.subagent-graph-checkpoint+json"
    ] = "application/vnd.pulsara.subagent-graph-checkpoint+json"
    content_sha256: str = Field(min_length=1)
    byte_count: int = Field(ge=1)
    semantic_metadata_fingerprint: str = Field(min_length=1)
    checkpoint_state: SubagentGraphCheckpointStateFact


class SubagentGraphCheckpointPolicyFact(FrozenLongHorizonFact):
    checkpoint_every_events: int = Field(default=512, ge=1)
    checkpoint_max_delta_events: int = Field(default=32_768, ge=0)
    checkpoint_max_delta_bytes: int = Field(default=33_554_432, ge=0)
    bootstrap_max_events: int = Field(default=2048, ge=1)
    bootstrap_max_bytes: int = Field(default=8_388_608, ge=1)
    rebase_max_checkpoint_candidates: int = Field(default=8, ge=1)
    retained_checkpoint_min_count: int = Field(default=2, ge=1)
    policy_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "SubagentGraphCheckpointPolicyFact":
        expected = context_fingerprint(
            "subagent-graph-checkpoint-policy:v1",
            self.model_dump(mode="json", exclude={"policy_fingerprint"}),
        )
        if self.policy_fingerprint != expected:
            raise ValueError("subagent graph checkpoint policy fingerprint mismatch")
        return self


class SubagentGraphCheckpointRepairOutcome(StrEnum):
    VERIFIED = "verified"
    REBUILT = "rebuilt"
    REDUCER_BINDING_UNAVAILABLE = "reducer_binding_unavailable"
    LEDGER_UNTRUSTED = "ledger_untrusted"
    ARTIFACT_CONFLICT = "artifact_conflict"


class LongHorizonContextAllocationPolicyFact(FrozenLongHorizonFact):
    schema_version: Literal["long_horizon_context_allocation.v1"] = (
        "long_horizon_context_allocation.v1"
    )
    tool_projection_soft_ratio_ppm: int = Field(gt=0, le=1_000_000)
    tool_projection_post_rewrite_ratio_ppm: int = Field(gt=0, le=1_000_000)
    window_compaction_trigger_ratio_ppm: int = Field(gt=0, le=1_000_000)
    window_compaction_post_target_ratio_ppm: int = Field(gt=0, le=1_000_000)
    latest_tool_result_reserve_tokens: int = Field(gt=0)
    current_run_recent_unit_count: int = Field(gt=0)
    max_projection_units_per_window: int = Field(gt=0)
    max_rollup_members: int = Field(gt=0, le=256)
    max_rewrite_entries_per_page: int = Field(gt=0, le=256)
    max_safe_point_revisions: int = Field(ge=4)
    max_compile_attempts_per_model_call: int = Field(ge=2)
    policy_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _policy(self) -> "LongHorizonContextAllocationPolicyFact":
        if not (
            self.tool_projection_post_rewrite_ratio_ppm
            <= self.tool_projection_soft_ratio_ppm
            < self.window_compaction_trigger_ratio_ppm
        ):
            raise ValueError("long-horizon projection ratios are inconsistent")
        if (
            self.window_compaction_post_target_ratio_ppm
            >= self.window_compaction_trigger_ratio_ppm
        ):
            raise ValueError("window post target must be below trigger")
        if self.max_rollup_members > self.max_projection_units_per_window:
            raise ValueError("max rollup members exceeds projection unit cap")
        _validate_fingerprint(
            self,
            namespace="long-horizon-context-allocation:v1",
            field_name="policy_fingerprint",
        )
        return self


class LongHorizonContextBudgetDecisionFact(FrozenLongHorizonFact):
    schema_version: Literal["long_horizon_context_budget_decision.v1"] = (
        "long_horizon_context_budget_decision.v1"
    )
    window_id: str = Field(min_length=1)
    source_through_sequence: int = Field(ge=0)
    input_budget_tokens: int = Field(gt=0)
    fixed_non_result_tokens: int = Field(ge=0)
    projected_tool_tokens_before: int = Field(ge=0)
    minimum_result_projection_tokens: int = Field(ge=0)
    soft_tool_projection_tokens: int = Field(ge=0)
    post_rewrite_target_tokens: int = Field(ge=0)
    projected_tool_tokens_after: int | None = Field(default=None, ge=0)
    final_input_tokens_after: int | None = Field(default=None, ge=0)
    active_projection_unit_count: int = Field(ge=0)
    max_projection_units_per_window: int = Field(gt=0)
    unit_count_limit_exceeded: bool
    decision: Literal[
        "within_soft_target",
        "projection_rewrite",
        "window_compaction_required",
        "protected_tail_unreachable",
    ]
    estimator_fingerprint: str = Field(min_length=1)
    decision_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _decision(self) -> "LongHorizonContextBudgetDecisionFact":
        hard_available = max(0, self.input_budget_tokens - self.fixed_non_result_tokens)
        if self.soft_tool_projection_tokens > hard_available:
            raise ValueError("soft tool projection target exceeds hard availability")
        if self.post_rewrite_target_tokens > self.soft_tool_projection_tokens:
            raise ValueError("post-rewrite target exceeds soft projection target")
        if self.minimum_result_projection_tokens > self.projected_tool_tokens_before:
            raise ValueError("minimum projection exceeds current projection")
        if self.unit_count_limit_exceeded != (
            self.active_projection_unit_count > self.max_projection_units_per_window
        ):
            raise ValueError("projection unit-count decision mismatch")
        if (self.projected_tool_tokens_after is None) != (
            self.final_input_tokens_after is None
        ):
            raise ValueError("post-decision token measurements must be paired")
        if self.final_input_tokens_after is not None and self.projected_tool_tokens_after is not None:
            if self.final_input_tokens_after != (
                self.fixed_non_result_tokens + self.projected_tool_tokens_after
            ):
                raise ValueError("final input token decomposition mismatch")
        if self.decision == "within_soft_target":
            if (
                self.projected_tool_tokens_after != self.projected_tool_tokens_before
                or self.final_input_tokens_after
                != self.fixed_non_result_tokens + self.projected_tool_tokens_before
            ):
                raise ValueError("within-target decision requires unchanged measurements")
        _validate_fingerprint(
            self,
            namespace="long-horizon-context-budget-decision:v1",
            field_name="decision_fingerprint",
        )
        return self


class ProjectionTargetUnreachableAuditFact(FrozenLongHorizonFact):
    """Durable planning outcome for a minimum projection above its target."""

    schema_version: Literal["projection_target_unreachable_audit.v1"] = (
        "projection_target_unreachable_audit.v1"
    )
    target_projected_tokens: int = Field(ge=0)
    minimum_projected_tokens: int = Field(ge=0)
    source_projection_generation: int = Field(ge=0)
    reason_code: Literal["projection_target_unreachable"] = (
        "projection_target_unreachable"
    )
    audit_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _audit(self) -> "ProjectionTargetUnreachableAuditFact":
        if self.minimum_projected_tokens <= self.target_projected_tokens:
            raise ValueError("unreachable projection must remain above its target")
        _validate_fingerprint(
            self,
            namespace="projection-target-unreachable-audit:v1",
            field_name="audit_fingerprint",
        )
        return self


class LongHorizonProjectionPressureShadowFact(FrozenLongHorizonFact):
    schema_version: Literal["long_horizon_projection_pressure_shadow.v1"] = (
        "long_horizon_projection_pressure_shadow.v1"
    )
    window_id: str = Field(min_length=1)
    source_through_sequence: int = Field(ge=0)
    active_projection_unit_count: int = Field(ge=0)
    max_projection_units_per_window: int = Field(gt=0)
    unit_count_limit_exceeded: bool
    enforcement_mode: Literal["diagnostic_only"] = "diagnostic_only"
    operational_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _shadow(self) -> "LongHorizonProjectionPressureShadowFact":
        if self.unit_count_limit_exceeded != (
            self.active_projection_unit_count > self.max_projection_units_per_window
        ):
            raise ValueError("projection pressure shadow count mismatch")
        _validate_fingerprint(
            self,
            namespace="long-horizon-projection-pressure-shadow:v1",
            field_name="operational_fingerprint",
        )
        return self


class RolloutBudgetPolicyFact(FrozenLongHorizonFact):
    schema_version: Literal["rollout_budget_policy.v1"] = "rollout_budget_policy.v1"
    total_input_budget_multiplier_milli: int = Field(gt=0)
    non_cached_input_weight_milli: int = Field(gt=0)
    cached_input_weight_milli: int = Field(gt=0)
    output_weight_milli: int = Field(gt=0)
    tool_cost_unit_weight_milli: int = Field(gt=0)
    finalization_reserved_model_calls: int = Field(ge=2)
    finalization_reserved_window_compactions: int = Field(ge=1)
    finalization_reserved_tool_cost_units: int = Field(ge=1)
    warning_consumption_ratio_ppm: int = Field(gt=0, le=1_000_000)
    restricted_consumption_ratio_ppm: int = Field(gt=0, le=1_000_000)
    finalization_consumption_ratio_ppm: int = Field(gt=0, le=1_000_000)
    emergency_model_call_limit: int = Field(gt=0)
    emergency_tool_call_limit: int = Field(gt=0)
    max_concurrent_subagent_reservations: int = Field(gt=0)
    policy_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _policy(self) -> "RolloutBudgetPolicyFact":
        if self.cached_input_weight_milli > self.non_cached_input_weight_milli:
            raise ValueError("cached input weight exceeds non-cached input weight")
        if not (
            self.warning_consumption_ratio_ppm
            < self.restricted_consumption_ratio_ppm
            < self.finalization_consumption_ratio_ppm
        ):
            raise ValueError("rollout phase thresholds must be strictly ordered")
        if self.emergency_model_call_limit <= self.finalization_reserved_model_calls:
            raise ValueError("emergency model-call limit is too small")
        if (
            self.emergency_tool_call_limit
            <= self.finalization_reserved_tool_cost_units
        ):
            raise ValueError("emergency tool-call limit is too small")
        _validate_fingerprint(
            self,
            namespace="rollout-budget-policy:v1",
            field_name="policy_fingerprint",
        )
        return self


class ChildRolloutReservationPolicyFact(FrozenLongHorizonFact):
    schema_version: Literal["child_rollout_reservation_policy.v1"] = (
        "child_rollout_reservation_policy.v1"
    )
    max_agent_model_calls_per_child: int = Field(gt=0)
    max_window_compactions_per_child: int = Field(gt=0)
    max_tool_cost_units_per_child: int = Field(gt=0)
    max_parent_exploration_share_ppm: int = Field(gt=0, le=1_000_000)
    policy_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _policy(self) -> "ChildRolloutReservationPolicyFact":
        _validate_fingerprint(
            self,
            namespace="child-rollout-reservation-policy:v1",
            field_name="policy_fingerprint",
        )
        return self


class RolloutStatusHintPolicyFact(FrozenLongHorizonFact):
    schema_version: Literal["rollout-status-hint-policy:v1"] = (
        "rollout-status-hint-policy:v1"
    )
    recent_tool_call_window: int = Field(gt=0)
    minimum_equivalent_outcome_occurrences: int = Field(ge=2)
    max_recurrence_entries: int = Field(gt=0)
    policy_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _policy(self) -> "RolloutStatusHintPolicyFact":
        if self.minimum_equivalent_outcome_occurrences > self.recent_tool_call_window:
            raise ValueError("recurrence threshold exceeds recent call window")
        _validate_fingerprint(
            self,
            namespace="rollout-status-hint-policy:v1",
            field_name="policy_fingerprint",
        )
        return self


class RolloutReservationReferenceFact(FrozenLongHorizonFact):
    owner_runtime_session_id: str = Field(min_length=1)
    reservation_id: str = Field(min_length=1)
    reservation_event_id: str = Field(min_length=1)
    reservation_sequence: int = Field(ge=1)
    reservation_fingerprint: str = Field(min_length=1)


class RunLongHorizonContractFact(FrozenLongHorizonFact):
    contract_version: Literal["run-long-horizon:v1"] = "run-long-horizon:v1"
    rollout_account_id: str = Field(min_length=1)
    rollout_account_owner_runtime_session_id: str = Field(min_length=1)
    rollout_account_owner_run_id: str = Field(min_length=1)
    inherited_rollout_reservation: RolloutReservationReferenceFact | None
    initial_window_id: str = Field(min_length=1)
    initial_window_open_event_id: str = Field(min_length=1)
    window_policy: LongHorizonContextAllocationPolicyFact
    window_compaction_summarizer_target: ResolvedModelTargetFact
    rollout_policy: RolloutBudgetPolicyFact
    child_rollout_policy: ChildRolloutReservationPolicyFact
    rollout_status_hint_policy: RolloutStatusHintPolicyFact
    subagent_graph_reducer_contract: SubagentGraphReducerContractFact
    contract_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _contract(self) -> "RunLongHorizonContractFact":
        _validate_fingerprint(
            self,
            namespace="run-long-horizon:v1",
            field_name="contract_fingerprint",
        )
        return self


class ContextWindowTranscriptBasisFact(FrozenLongHorizonFact):
    basis_kind: Literal["initial_run", "window_compaction"]
    run_start_event_id: str = Field(min_length=1)
    source_compaction_started_event_id: str | None
    source_compaction_plan_fingerprint: str | None
    source_through_sequence_at_compaction: int | None = Field(default=None, ge=1)
    summarized_pair_groups_fingerprint: str | None
    retained_pair_groups_fingerprint: str | None
    basis_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _basis(self) -> "ContextWindowTranscriptBasisFact":
        compact_fields = (
            self.source_compaction_started_event_id,
            self.source_compaction_plan_fingerprint,
            self.source_through_sequence_at_compaction,
            self.summarized_pair_groups_fingerprint,
            self.retained_pair_groups_fingerprint,
        )
        if self.basis_kind == "initial_run" and any(
            value is not None for value in compact_fields
        ):
            raise ValueError("initial transcript basis cannot carry compaction facts")
        if self.basis_kind == "window_compaction" and any(
            value is None for value in compact_fields
        ):
            raise ValueError("compacted transcript basis is incomplete")
        _validate_fingerprint(
            self,
            namespace="context-window-transcript-basis:v1",
            field_name="basis_fingerprint",
        )
        return self


class ContextWindowFact(FrozenLongHorizonFact):
    contract_version: Literal["context-window:v1"] = "context-window:v1"
    window_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    generation: int = Field(ge=1)
    previous_window_id: str | None
    open_reason: ContextWindowOpenReason
    transcript_basis: ContextWindowTranscriptBasisFact
    source_through_sequence_at_open: int = Field(ge=0)
    resolved_model_target_fingerprint: str = Field(min_length=1)
    input_budget_tokens: int = Field(ge=1)
    token_estimator_fingerprint: str = Field(min_length=1)
    window_policy_fingerprint: str = Field(min_length=1)
    initial_projection_generation: Literal[0] = 0
    initial_projection_unit_count: int = Field(ge=0)
    initial_projection_state_fingerprint: str = Field(min_length=1)
    stable_close_event_id: str = Field(min_length=1)
    source_compaction_id: str | None
    source_summary_artifact_id: str | None
    source_summary_fingerprint: str | None
    window_semantic_fingerprint: str = Field(min_length=1)
    window_fact_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _window(self) -> "ContextWindowFact":
        compact_fields = (
            self.previous_window_id,
            self.source_compaction_id,
            self.source_summary_artifact_id,
            self.source_summary_fingerprint,
        )
        if self.generation == 1:
            if self.open_reason is not ContextWindowOpenReason.INITIAL_RUN:
                raise ValueError("initial window requires initial_run reason")
            if any(value is not None for value in compact_fields):
                raise ValueError("initial window cannot carry compaction attribution")
            if self.transcript_basis.basis_kind != "initial_run":
                raise ValueError("initial window requires initial transcript basis")
        else:
            if self.open_reason is not ContextWindowOpenReason.LLM_COMPACTION:
                raise ValueError("subsequent window requires llm_compaction reason")
            if any(value is None for value in compact_fields):
                raise ValueError("compacted window attribution is incomplete")
            if self.transcript_basis.basis_kind != "window_compaction":
                raise ValueError("compacted window requires compacted transcript basis")
        semantic_payload = self.model_dump(
            mode="json",
            exclude={
                "window_id",
                "stable_close_event_id",
                "window_semantic_fingerprint",
                "window_fact_fingerprint",
            },
        )
        if self.window_semantic_fingerprint != context_fingerprint(
            "context-window-semantic:v1", semantic_payload
        ):
            raise ValueError("context window semantic fingerprint mismatch")
        _validate_fingerprint(
            self,
            namespace="context-window-fact:v1",
            field_name="window_fact_fingerprint",
        )
        return self


class ToolObservationProjectionFact(FrozenLongHorizonFact):
    schema_version: Literal["tool_observation_projection.v1"] = (
        "tool_observation_projection.v1"
    )
    window_id: str = Field(min_length=1)
    projection_generation: int = Field(ge=0)
    unit_id: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    tool_result_event_id: str = Field(min_length=1)
    tool_result_sequence: int = Field(ge=1)
    tool_name: str = Field(min_length=1)
    representation: ToolObservationRepresentation
    representation_rank: int = Field(gt=0)
    rendered_fragment_artifact_id: str | None
    rendered_fragment_fingerprint: str = Field(min_length=1)
    estimated_tokens: int = Field(ge=0)
    primary_artifact_id: str | None
    essential_envelope_fingerprint: str = Field(min_length=1)
    observation_timing_fingerprint: str = Field(min_length=1)
    source_rollup_id: str | None
    protected_reason_codes: tuple[str, ...]
    decision_reason_code: ProjectionRewriteReason
    semantic_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _projection(self) -> "ToolObservationProjectionFact":
        if self.representation_rank != TOOL_OBSERVATION_REPRESENTATION_RANK[
            self.representation
        ]:
            raise ValueError("tool observation representation rank mismatch")
        if tuple(sorted(set(self.protected_reason_codes))) != self.protected_reason_codes:
            raise ValueError("protected reason codes must be sorted and unique")
        if (
            self.representation is ToolObservationRepresentation.ROLLUP_MEMBER
        ) != (self.source_rollup_id is not None):
            raise ValueError("rollup member representation requires source rollup")
        _validate_fingerprint(
            self,
            namespace="tool-observation-projection:v1",
            field_name="semantic_fingerprint",
        )
        return self


class ObservationRollupMemberFact(FrozenLongHorizonFact):
    unit_id: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    result_event_id: str = Field(min_length=1)
    result_sequence: int = Field(ge=1)
    result_state: Literal["success", "error", "interrupted", "denied"]
    essential_semantic_fingerprint: str = Field(min_length=1)
    primary_artifact_id: str | None


class ObservationRollupFact(FrozenLongHorizonFact):
    schema_version: Literal["observation_rollup.v1"] = "observation_rollup.v1"
    rollup_id: str = Field(min_length=1)
    window_id: str = Field(min_length=1)
    rollup_kind: Literal[
        "repeated_search_results",
        "repeated_file_reads",
        "terminal_inventory",
        "repeated_error_family",
        "subagent_result_index",
    ]
    member_facts: tuple[ObservationRollupMemberFact, ...] = Field(min_length=2)
    ordered_member_set_fingerprint: str = Field(min_length=1)
    renderer_id: str = Field(min_length=1)
    renderer_version: str = Field(min_length=1)
    renderer_contract_fingerprint: str = Field(min_length=1)
    rendered_artifact_id: str = Field(min_length=1)
    rendered_content_sha256: str = Field(min_length=1)
    estimated_tokens: int = Field(ge=0)
    evidence_keys: tuple[str, ...]
    semantic_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _rollup(self) -> "ObservationRollupFact":
        sequences = tuple(item.result_sequence for item in self.member_facts)
        unit_ids = tuple(item.unit_id for item in self.member_facts)
        if sequences != tuple(sorted(sequences)) or len(unit_ids) != len(set(unit_ids)):
            raise ValueError("rollup members must be ordered and unique")
        member_identity = tuple(
            (
                item.unit_id,
                item.tool_call_id,
                item.result_event_id,
                item.essential_semantic_fingerprint,
            )
            for item in self.member_facts
        )
        if self.ordered_member_set_fingerprint != context_fingerprint(
            "observation-rollup-members:v1", member_identity
        ):
            raise ValueError("rollup member set fingerprint mismatch")
        if tuple(sorted(set(self.evidence_keys))) != self.evidence_keys:
            raise ValueError("rollup evidence keys must be sorted and unique")
        if len(self.evidence_keys) > 64 or any(
            not value or len(value) > 256 for value in self.evidence_keys
        ):
            raise ValueError("rollup evidence keys exceed bounds")
        expected_rollup_id = observation_rollup_id(
            window_id=self.window_id,
            rollup_kind=self.rollup_kind,
            ordered_member_set_fingerprint=self.ordered_member_set_fingerprint,
            renderer_contract_fingerprint=self.renderer_contract_fingerprint,
        )
        if self.rollup_id != expected_rollup_id:
            raise ValueError("rollup ID does not match its semantic members")
        _validate_fingerprint(
            self,
            namespace="observation-rollup:v1",
            field_name="semantic_fingerprint",
        )
        return self


class ObservationRollupRendererContractFact(FrozenLongHorizonFact):
    schema_version: Literal["observation_rollup_renderer_contract.v1"] = (
        "observation_rollup_renderer_contract.v1"
    )
    renderer_id: str = Field(min_length=1)
    renderer_version: str = Field(min_length=1)
    input_schema_fingerprint: str = Field(min_length=1)
    output_schema_fingerprint: str = Field(min_length=1)
    framing_policy_fingerprint: str = Field(min_length=1)
    placement_contract_fingerprint: str = Field(min_length=1)
    renderer_contract_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _contract(self) -> "ObservationRollupRendererContractFact":
        _validate_fingerprint(
            self,
            namespace="observation-rollup-renderer-contract:v1",
            field_name="renderer_contract_fingerprint",
        )
        return self


class ObservationRollupPlacementAnchorFact(FrozenLongHorizonFact):
    placement: Literal["after_complete_pair_group"] = "after_complete_pair_group"
    pair_group_id: str = Field(min_length=1)
    insert_after_transcript_message_id: str = Field(min_length=1)
    insert_after_source_sequence: int = Field(ge=1)
    anchor_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _anchor(self) -> "ObservationRollupPlacementAnchorFact":
        _validate_fingerprint(
            self,
            namespace="observation-rollup-placement-anchor:v1",
            field_name="anchor_fingerprint",
        )
        return self


class RuntimeDerivedObservationCompileUnit(FrozenLongHorizonFact):
    unit_id: str = Field(min_length=1)
    source_kind: Literal["long_horizon_observation_rollup"] = (
        "long_horizon_observation_rollup"
    )
    source_semantic_fingerprint: str = Field(min_length=1)
    inline_text: str = Field(min_length=1)
    inline_content_sha256: str = Field(min_length=1)
    inline_chars: int = Field(ge=1)
    placement_anchor: ObservationRollupPlacementAnchorFact
    lowering_kind: Literal["runtime_owned_derived_observation"] = (
        "runtime_owned_derived_observation"
    )
    carrier_contract_fingerprint: str = Field(min_length=1)
    unit_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _compile_unit(self) -> "RuntimeDerivedObservationCompileUnit":
        if self.inline_chars != len(self.inline_text):
            raise ValueError("runtime observation inline char count mismatch")
        digest = f"sha256:{sha256(self.inline_text.encode('utf-8')).hexdigest()}"
        if self.inline_content_sha256 != digest:
            raise ValueError("runtime observation inline content hash mismatch")
        _validate_fingerprint(
            self,
            namespace="runtime-derived-observation-compile-unit:v1",
            field_name="unit_fingerprint",
        )
        return self


class PreparedObservationRollupUnit(FrozenLongHorizonFact):
    schema_version: Literal["prepared_observation_rollup.v1"] = (
        "prepared_observation_rollup.v1"
    )
    rollup: ObservationRollupFact
    artifact_id: str = Field(min_length=1)
    artifact_content_sha256: str = Field(min_length=1)
    ordered_member_unit_ids: tuple[str, ...]
    ordered_member_set_fingerprint: str = Field(min_length=1)
    compile_unit: RuntimeDerivedObservationCompileUnit
    prepared_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _prepared(self) -> "PreparedObservationRollupUnit":
        if self.artifact_id != self.rollup.rendered_artifact_id:
            raise ValueError("prepared rollup artifact ID mismatch")
        if self.artifact_content_sha256 != self.rollup.rendered_content_sha256:
            raise ValueError("prepared rollup artifact hash mismatch")
        if self.compile_unit.inline_content_sha256 != self.artifact_content_sha256:
            raise ValueError("prepared rollup inline/artifact hash mismatch")
        if self.compile_unit.source_semantic_fingerprint != self.rollup.semantic_fingerprint:
            raise ValueError("prepared rollup source fingerprint mismatch")
        member_ids = tuple(item.unit_id for item in self.rollup.member_facts)
        if self.ordered_member_unit_ids != member_ids:
            raise ValueError("prepared rollup member order mismatch")
        if self.ordered_member_set_fingerprint != (
            self.rollup.ordered_member_set_fingerprint
        ):
            raise ValueError("prepared rollup member fingerprint mismatch")
        _validate_fingerprint(
            self,
            namespace="prepared-observation-rollup:v1",
            field_name="prepared_fingerprint",
        )
        return self


def default_observation_rollup_renderer_contract() -> (
    ObservationRollupRendererContractFact
):
    payload = {
        "schema_version": "observation_rollup_renderer_contract.v1",
        "renderer_id": "pulsara.observation_rollup.canonical",
        "renderer_version": "v1",
        "input_schema_fingerprint": "schema:tool-result-rollup-semantics:v1",
        "output_schema_fingerprint": "schema:observation-rollup:v1",
        "framing_policy_fingerprint": context_fingerprint(
            "observation-rollup-framing-policy:v1",
            {"format": "canonical_markdown", "bounded_evidence": True},
        ),
        "placement_contract_fingerprint": context_fingerprint(
            "observation-rollup-placement-policy:v1",
            {"placement": "after_complete_pair_group"},
        ),
    }
    return ObservationRollupRendererContractFact(
        **payload,
        renderer_contract_fingerprint=context_fingerprint(
            "observation-rollup-renderer-contract:v1", payload
        ),
    )


def observation_rollup_id(
    *,
    window_id: str,
    rollup_kind: str,
    ordered_member_set_fingerprint: str,
    renderer_contract_fingerprint: str,
) -> str:
    digest = context_fingerprint(
        "observation-rollup-id:v1",
        {
            "window_id": window_id,
            "rollup_kind": rollup_kind,
            "ordered_member_set_fingerprint": ordered_member_set_fingerprint,
            "renderer_contract_fingerprint": renderer_contract_fingerprint,
        },
    ).removeprefix("sha256:")
    return f"observation_rollup:{digest}"


class ContextWindowProjectionState(FrozenLongHorizonFact):
    window_id: str = Field(min_length=1)
    window_generation: int = Field(ge=1)
    projection_generation: int = Field(ge=0)
    through_sequence: int = Field(ge=0)
    unit_projections: tuple[ToolObservationProjectionFact, ...]
    rollups: tuple[ObservationRollupFact, ...]
    total_projected_tokens: int = Field(ge=0)
    protected_projected_tokens: int = Field(ge=0)
    state_semantic_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _state(self) -> "ContextWindowProjectionState":
        if self.protected_projected_tokens > self.total_projected_tokens:
            raise ValueError("protected projection tokens exceed total")
        unit_ids = tuple(item.unit_id for item in self.unit_projections)
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("projection contains duplicate unit IDs")
        if any(item.window_id != self.window_id for item in self.unit_projections):
            raise ValueError("projection unit window mismatch")
        if any(
            item.projection_generation != self.projection_generation
            for item in self.unit_projections
        ):
            raise ValueError("projection unit generation mismatch")
        if (
            sum(item.estimated_tokens for item in self.unit_projections)
            + sum(item.estimated_tokens for item in self.rollups)
            != self.total_projected_tokens
        ):
            raise ValueError("projection total token count mismatch")
        expected = context_fingerprint(
            "context-window-projection-state:v1",
            {
                "projection_generation": self.projection_generation,
                "unit_projections": self.unit_projections,
                "rollups": self.rollups,
            },
        )
        if self.state_semantic_fingerprint != expected:
            raise ValueError("context window projection state fingerprint mismatch")
        return self


class ToolObservationProjectionRewriteEntryFact(FrozenLongHorizonFact):
    unit_id: str = Field(min_length=1)
    from_representation: ToolObservationRepresentation | None
    to_projection: ToolObservationProjectionFact

    @model_validator(mode="after")
    def _identity(self) -> "ToolObservationProjectionRewriteEntryFact":
        if self.unit_id != self.to_projection.unit_id:
            raise ValueError("projection rewrite entry unit mismatch")
        if self.from_representation is not None:
            old_rank = TOOL_OBSERVATION_REPRESENTATION_RANK[
                self.from_representation
            ]
            if self.to_projection.representation_rank > old_rank:
                raise ValueError("projection rewrite cannot increase representation rank")
        return self


class ToolObservationProtectionFact(FrozenLongHorizonFact):
    unit_id: str = Field(min_length=1)
    classes: tuple[
        Literal[
            "current_user_adjacent",
            "current_run_recent",
            "pending_interaction",
            "error_recovery",
            "explicit_user_requested_evidence",
            "unconsumed_subagent_result",
            "artifact_write_pending",
            "tool_call_in_flight",
        ],
        ...,
    ]
    minimum_representation: ToolObservationRepresentation
    protection_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _protection(self) -> "ToolObservationProtectionFact":
        if tuple(sorted(set(self.classes))) != self.classes:
            raise ValueError("protection classes must be sorted and unique")
        _validate_fingerprint(
            self,
            namespace="tool-observation-protection:v1",
            field_name="protection_fingerprint",
        )
        return self


class ModelCallReservationQuoteFact(FrozenLongHorizonFact):
    resolved_model_call_id: str | None
    target_fingerprint: str = Field(min_length=1)
    physical_input_token_upper_bound: int = Field(ge=1)
    output_token_upper_bound: int = Field(ge=1)
    non_cached_input_weight_milli: int = Field(gt=0)
    output_weight_milli: int = Field(gt=0)
    reserved_milliunits: int = Field(gt=0)
    policy_fingerprint: str = Field(min_length=1)
    quote_semantic_fingerprint: str = Field(min_length=1)
    quote_fact_fingerprint: str | None

    @model_validator(mode="after")
    def _quote(self) -> "ModelCallReservationQuoteFact":
        expected = (
            self.physical_input_token_upper_bound
            * self.non_cached_input_weight_milli
            + self.output_token_upper_bound * self.output_weight_milli
        )
        if self.reserved_milliunits != expected:
            raise ValueError("model call reservation quote amount mismatch")
        semantic_payload = self.model_dump(
            mode="json",
            exclude={
                "resolved_model_call_id",
                "reserved_milliunits",
                "quote_semantic_fingerprint",
                "quote_fact_fingerprint",
            },
        )
        if self.quote_semantic_fingerprint != context_fingerprint(
            "model-call-reservation-quote-semantic:v1", semantic_payload
        ):
            raise ValueError("model call quote semantic fingerprint mismatch")
        if self.resolved_model_call_id is None:
            if self.quote_fact_fingerprint is not None:
                raise ValueError("configuration quote cannot carry fact fingerprint")
        else:
            expected_fact = context_fingerprint(
                "model-call-reservation-quote-fact:v1",
                {
                    "resolved_model_call_id": self.resolved_model_call_id,
                    "quote_semantic_fingerprint": self.quote_semantic_fingerprint,
                },
            )
            if self.quote_fact_fingerprint != expected_fact:
                raise ValueError("model call quote fact fingerprint mismatch")
        return self


class RolloutUsageChargeFact(FrozenLongHorizonFact):
    accounting_basis: Literal[
        "provider_reported_usage",
        "not_started_zero",
        "reserved_missing_usage",
        "cancelled_reserved",
    ]
    reported_input_tokens: int | None = Field(default=None, ge=0)
    reported_cached_input_tokens: int | None = Field(default=None, ge=0)
    reported_output_tokens: int | None = Field(default=None, ge=0)
    pre_send_estimated_input_tokens: int = Field(ge=0)
    physical_input_token_upper_bound: int = Field(ge=1)
    output_token_upper_bound: int = Field(ge=1)
    charged_output_tokens: int = Field(ge=0)
    charged_milliunits: int = Field(ge=0)
    reservation_quote_fact_fingerprint: str = Field(min_length=1)
    policy_fingerprint: str = Field(min_length=1)
    charge_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _charge(self) -> "RolloutUsageChargeFact":
        reported = (
            self.reported_input_tokens,
            self.reported_cached_input_tokens,
            self.reported_output_tokens,
        )
        if self.accounting_basis == "provider_reported_usage":
            if any(value is None for value in reported):
                raise ValueError("reported usage charge requires all token fields")
            assert self.reported_input_tokens is not None
            assert self.reported_cached_input_tokens is not None
            assert self.reported_output_tokens is not None
            if self.reported_cached_input_tokens > self.reported_input_tokens:
                raise ValueError("cached usage exceeds input usage")
            if self.reported_input_tokens > self.physical_input_token_upper_bound:
                raise ValueError("reported input exceeds physical bound")
            if self.reported_output_tokens > self.output_token_upper_bound:
                raise ValueError("reported output exceeds physical bound")
            if self.charged_output_tokens != self.reported_output_tokens:
                raise ValueError("charged output must equal reported output")
        elif any(value is not None for value in reported):
            raise ValueError("non-reported usage basis cannot carry reported tokens")
        if self.accounting_basis == "not_started_zero":
            if self.charged_output_tokens != 0 or self.charged_milliunits != 0:
                raise ValueError("not-started usage must charge zero")
        elif self.accounting_basis in {
            "reserved_missing_usage",
            "cancelled_reserved",
        } and self.charged_output_tokens != self.output_token_upper_bound:
            raise ValueError("reserved usage must charge full output bound")
        _validate_fingerprint(
            self,
            namespace="rollout-usage-charge:v1",
            field_name="charge_fingerprint",
        )
        return self


class RolloutBudgetAccountFact(FrozenLongHorizonFact):
    account_id: str = Field(min_length=1)
    owner_runtime_session_id: str = Field(min_length=1)
    root_run_id: str = Field(min_length=1)
    policy: RolloutBudgetPolicyFact
    total_budget_milliunits: int = Field(gt=0)
    finalization_reserve_milliunits: int = Field(gt=0)
    finalization_agent_reserve_milliunits: int = Field(gt=0)
    finalization_compaction_reserve_milliunits: int = Field(gt=0)
    finalization_tool_reserve_milliunits: int = Field(gt=0)
    exploration_allowance_milliunits: int = Field(gt=0)
    semantic_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _account(self) -> "RolloutBudgetAccountFact":
        reserve = (
            self.finalization_agent_reserve_milliunits
            + self.finalization_compaction_reserve_milliunits
            + self.finalization_tool_reserve_milliunits
        )
        if self.finalization_reserve_milliunits != reserve:
            raise ValueError("finalization reserve breakdown mismatch")
        if self.total_budget_milliunits != reserve + self.exploration_allowance_milliunits:
            raise ValueError("rollout account total mismatch")
        _validate_fingerprint(
            self,
            namespace="rollout-budget-account:v1",
            field_name="semantic_fingerprint",
        )
        return self


class RolloutReservationFact(FrozenLongHorizonFact):
    reservation_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    owner_kind: Literal["model_call", "tool_call", "subagent_run"]
    owner_id: str = Field(min_length=1)
    phase_at_reservation: RolloutPhase
    budget_bucket: RolloutBudgetBucket
    reserved_milliunits: int = Field(gt=0)
    model_call_reservation_quote: ModelCallReservationQuoteFact | None
    source_sequence: int = Field(ge=0)
    semantic_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _reservation(self) -> "RolloutReservationFact":
        if self.owner_kind == "model_call":
            quote = self.model_call_reservation_quote
            if quote is None or quote.resolved_model_call_id != self.owner_id:
                raise ValueError("model reservation requires matching call quote")
            if quote.quote_fact_fingerprint is None:
                raise ValueError("model reservation quote must be call-bound")
            if self.reserved_milliunits != quote.reserved_milliunits:
                raise ValueError("model reservation amount differs from quote")
        elif self.model_call_reservation_quote is not None:
            raise ValueError("tool/child reservation cannot carry model quote")
        _validate_fingerprint(
            self,
            namespace="rollout-reservation:v1",
            field_name="semantic_fingerprint",
        )
        return self


class RolloutBudgetStateFact(FrozenLongHorizonFact):
    account_id: str = Field(min_length=1)
    phase: RolloutPhase
    charged_milliunits: int = Field(ge=0)
    reserved_milliunits: int = Field(ge=0)
    exploration_charged_milliunits: int = Field(ge=0)
    exploration_reserved_milliunits: int = Field(ge=0)
    finalization_agent_charged_milliunits: int = Field(ge=0)
    finalization_agent_reserved_milliunits: int = Field(ge=0)
    finalization_compaction_charged_milliunits: int = Field(ge=0)
    finalization_compaction_reserved_milliunits: int = Field(ge=0)
    finalization_tool_charged_milliunits: int = Field(ge=0)
    finalization_tool_reserved_milliunits: int = Field(ge=0)
    model_call_count: int = Field(ge=0)
    recovered_incomplete_model_stream_count: int = Field(ge=0)
    model_stream_reconciliation_blocker_count: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    active_reservations: tuple[RolloutReservationFact, ...]
    through_sequence: int = Field(ge=0)
    state_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _state(self) -> "RolloutBudgetStateFact":
        charged = (
            self.exploration_charged_milliunits
            + self.finalization_agent_charged_milliunits
            + self.finalization_compaction_charged_milliunits
            + self.finalization_tool_charged_milliunits
        )
        reserved = (
            self.exploration_reserved_milliunits
            + self.finalization_agent_reserved_milliunits
            + self.finalization_compaction_reserved_milliunits
            + self.finalization_tool_reserved_milliunits
        )
        if self.charged_milliunits != charged or self.reserved_milliunits != reserved:
            raise ValueError("rollout state bucket totals mismatch")
        ids = tuple(item.reservation_id for item in self.active_reservations)
        if len(ids) != len(set(ids)):
            raise ValueError("rollout state contains duplicate reservations")
        if sum(item.reserved_milliunits for item in self.active_reservations) != reserved:
            raise ValueError("active reservation total mismatch")
        if any(item.account_id != self.account_id for item in self.active_reservations):
            raise ValueError("active reservation account mismatch")
        _validate_fingerprint(
            self,
            namespace="rollout-budget-state:v1",
            field_name="state_fingerprint",
        )
        return self


class ContextWindowCompactionPlanFact(FrozenLongHorizonFact):
    schema_version: Literal["context-window-compaction-plan.v1"] = (
        "context-window-compaction-plan.v1"
    )
    compaction_id: str = Field(min_length=1)
    compaction_attempt_index: int = Field(ge=1)
    run_id: str = Field(min_length=1)
    source_window_id: str = Field(min_length=1)
    source_window_generation: int = Field(ge=1)
    source_projection_generation: int = Field(ge=0)
    source_projection_state_fingerprint: str = Field(min_length=1)
    source_through_sequence: int = Field(ge=1)
    target_window_id: str = Field(min_length=1)
    target_window_generation: int = Field(ge=2)
    source_context_fingerprint: str = Field(min_length=1)
    summarizer_call: ResolvedModelCallFact
    rollout_reservation: RolloutReservationFact
    summarizer_input_manifest_artifact_id: str = Field(min_length=1)
    summarizer_input_manifest_fingerprint: str = Field(min_length=1)
    source_document_artifact_id: str = Field(min_length=1)
    source_document_fingerprint: str = Field(min_length=1)
    protected_unit_ids: tuple[str, ...]
    summarized_unit_ids: tuple[str, ...]
    retained_tail_unit_ids: tuple[str, ...]
    summarized_message_ids: tuple[str, ...]
    retained_message_ids: tuple[str, ...]
    summarized_pair_group_ids: tuple[str, ...]
    retained_pair_group_ids: tuple[str, ...]
    estimated_tokens_before: int = Field(ge=0)
    fixed_new_window_tokens: int = Field(ge=0)
    protected_tail_tokens: int = Field(ge=0)
    summarizer_input_estimated_tokens: int = Field(ge=0)
    summary_output_budget_tokens: int = Field(ge=1)
    post_compaction_target_tokens: int = Field(ge=1)
    stable_started_event_id: str = Field(min_length=1)
    stable_completed_event_id: str = Field(min_length=1)
    stable_failed_event_id: str = Field(min_length=1)
    stable_source_window_close_event_id: str = Field(min_length=1)
    stable_target_window_open_event_id: str = Field(min_length=1)
    stable_target_window_close_event_id: str = Field(min_length=1)
    plan_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _plan(self) -> "ContextWindowCompactionPlanFact":
        if self.target_window_generation != self.source_window_generation + 1:
            raise ValueError("window compaction target generation is not contiguous")
        if self.source_window_id == self.target_window_id:
            raise ValueError("window compaction cannot reuse the source window ID")
        stable_ids = (
            self.stable_started_event_id,
            self.stable_completed_event_id,
            self.stable_failed_event_id,
            self.stable_source_window_close_event_id,
            self.stable_target_window_open_event_id,
            self.stable_target_window_close_event_id,
        )
        if len(stable_ids) != len(set(stable_ids)):
            raise ValueError("window compaction stable identities overlap")
        if (
            self.summarizer_call.purpose
            is not ModelCallPurpose.CONTEXT_WINDOW_COMPACTION_SUMMARY
        ):
            raise ValueError("window compaction requires its dedicated model purpose")
        reservation = self.rollout_reservation
        if (
            reservation.owner_kind != "model_call"
            or reservation.owner_id
            != self.summarizer_call.resolved_model_call_id
            or reservation.model_call_reservation_quote is None
        ):
            raise ValueError("window compaction reservation/call mismatch")
        if (
            self.summary_output_budget_tokens
            != self.summarizer_call.target.context_budget.effective_output_tokens
        ):
            raise ValueError("window compaction summary output cap drift")
        if (
            self.fixed_new_window_tokens
            + self.protected_tail_tokens
            + self.summary_output_budget_tokens
            > self.post_compaction_target_tokens
        ):
            raise ValueError("window compaction plan cannot fit its conservative target")
        protected = set(self.protected_unit_ids)
        summarized = set(self.summarized_unit_ids)
        retained = set(self.retained_tail_unit_ids)
        if len(protected) != len(self.protected_unit_ids):
            raise ValueError("window compaction protected units are duplicated")
        if len(summarized) != len(self.summarized_unit_ids):
            raise ValueError("window compaction summarized units are duplicated")
        if len(retained) != len(self.retained_tail_unit_ids):
            raise ValueError("window compaction retained units are duplicated")
        if summarized & retained:
            raise ValueError("window compaction unit partitions overlap")
        if not protected <= retained:
            raise ValueError("protected window units must remain in the retained tail")
        summarized_messages = set(self.summarized_message_ids)
        retained_messages = set(self.retained_message_ids)
        if (
            not summarized_messages
            or not retained_messages
            or summarized_messages & retained_messages
            or len(summarized_messages) != len(self.summarized_message_ids)
            or len(retained_messages) != len(self.retained_message_ids)
        ):
            raise ValueError("window compaction message partitions are invalid")
        if len(set(self.summarized_pair_group_ids)) != len(
            self.summarized_pair_group_ids
        ) or len(set(self.retained_pair_group_ids)) != len(
            self.retained_pair_group_ids
        ):
            raise ValueError("window compaction pair groups are duplicated")
        if set(self.summarized_pair_group_ids) & set(self.retained_pair_group_ids):
            raise ValueError("window compaction pair-group partitions overlap")
        _validate_fingerprint(
            self,
            namespace="context-window-compaction-plan:v1",
            field_name="plan_fingerprint",
        )
        return self


class ChildRolloutSettlementAggregateFact(FrozenLongHorizonFact):
    subaccount_fingerprint: str = Field(min_length=1)
    provider_reported_model_call_count: int = Field(ge=0)
    reserved_missing_model_call_count: int = Field(ge=0)
    cancelled_reserved_model_call_count: int = Field(ge=0)
    not_started_zero_model_call_count: int = Field(ge=0)
    tool_terminal_settlement_count: int = Field(ge=0)
    model_call_count: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    reported_subset_input_tokens: int = Field(ge=0)
    reported_subset_cached_input_tokens: int = Field(ge=0)
    reported_subset_output_tokens: int = Field(ge=0)
    model_charged_milliunits: int = Field(ge=0)
    tool_charged_milliunits: int = Field(ge=0)
    charged_milliunits: int = Field(ge=0)
    through_sequence: int = Field(ge=1)
    aggregate_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _aggregate(self) -> "ChildRolloutSettlementAggregateFact":
        model_count = (
            self.provider_reported_model_call_count
            + self.reserved_missing_model_call_count
            + self.cancelled_reserved_model_call_count
            + self.not_started_zero_model_call_count
        )
        if self.model_call_count != model_count:
            raise ValueError("child model settlement count mismatch")
        if self.tool_call_count != self.tool_terminal_settlement_count:
            raise ValueError("child tool settlement count mismatch")
        if self.reported_subset_cached_input_tokens > self.reported_subset_input_tokens:
            raise ValueError("child cached input exceeds reported input")
        if self.charged_milliunits != (
            self.model_charged_milliunits + self.tool_charged_milliunits
        ):
            raise ValueError("child settlement charge mismatch")
        _validate_fingerprint(
            self,
            namespace="child-rollout-settlement-aggregate:v1",
            field_name="aggregate_fingerprint",
        )
        return self


class ChildRolloutUsageHandoffFact(FrozenLongHorizonFact):
    subaccount_fingerprint: str = Field(min_length=1)
    settlement_aggregate: ChildRolloutSettlementAggregateFact
    child_terminal_reference: ChildNativeTerminalReferenceFact
    handoff_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _handoff(self) -> "ChildRolloutUsageHandoffFact":
        if (
            self.subaccount_fingerprint
            != self.settlement_aggregate.subaccount_fingerprint
        ):
            raise ValueError("child rollout handoff subaccount mismatch")
        _validate_fingerprint(
            self,
            namespace="child-rollout-usage-handoff:v1",
            field_name="handoff_fingerprint",
        )
        return self


def build_child_rollout_usage_handoff(
    *,
    settlement_aggregate: ChildRolloutSettlementAggregateFact,
    child_terminal_reference: ChildNativeTerminalReferenceFact,
) -> ChildRolloutUsageHandoffFact:
    payload = {
        "subaccount_fingerprint": settlement_aggregate.subaccount_fingerprint,
        "settlement_aggregate": settlement_aggregate,
        "child_terminal_reference": child_terminal_reference,
    }
    return ChildRolloutUsageHandoffFact(
        **payload,
        handoff_fingerprint=context_fingerprint(
            "child-rollout-usage-handoff:v1", payload
        ),
    )


class ResolvedChildRolloutBudgetFact(FrozenLongHorizonFact):
    child_profile: str = Field(min_length=1)
    child_primary_target_fingerprint: str = Field(min_length=1)
    child_summarizer_target_fingerprint: str = Field(min_length=1)
    child_window_policy_fingerprint: str = Field(min_length=1)
    child_policy_fingerprint: str = Field(min_length=1)
    child_primary_reservation_quote_semantic_fingerprint: str = Field(min_length=1)
    child_compaction_reservation_quote_semantic_fingerprint: str = Field(min_length=1)
    one_agent_call_reserve_milliunits: int = Field(gt=0)
    one_compaction_call_reserve_milliunits: int = Field(gt=0)
    tool_reserve_milliunits: int = Field(gt=0)
    profile_limit_milliunits: int = Field(gt=0)
    parent_share_limit_milliunits: int = Field(gt=0)
    max_rollout_milliunits_per_child: int = Field(gt=0)
    parent_account_state_fingerprint: str = Field(min_length=1)
    resolution_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _budget(self) -> "ResolvedChildRolloutBudgetFact":
        if self.max_rollout_milliunits_per_child != min(
            self.profile_limit_milliunits, self.parent_share_limit_milliunits
        ):
            raise ValueError("resolved child rollout limit mismatch")
        _validate_fingerprint(
            self,
            namespace="resolved-child-rollout-budget:v1",
            field_name="resolution_fingerprint",
        )
        return self


class ChildRolloutSubaccountFact(FrozenLongHorizonFact):
    root_account_id: str = Field(min_length=1)
    parent_reservation: RolloutReservationReferenceFact
    child_runtime_session_id: str = Field(min_length=1)
    child_run_id: str = Field(min_length=1)
    resolved_budget: ResolvedChildRolloutBudgetFact
    reserved_milliunits: int = Field(gt=0)
    subaccount_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _subaccount(self) -> "ChildRolloutSubaccountFact":
        if (
            self.reserved_milliunits
            != self.resolved_budget.max_rollout_milliunits_per_child
        ):
            raise ValueError("child subaccount reservation amount mismatch")
        _validate_fingerprint(
            self,
            namespace="child-rollout-subaccount:v1",
            field_name="subaccount_fingerprint",
        )
        return self


class RecentToolActionRecurrenceFact(FrozenLongHorizonFact):
    normalized_action_fingerprint: str = Field(min_length=1)
    terminal_outcome_fingerprint: str = Field(min_length=1)
    action_class: LongHorizonActionClass
    action_occurrence_count: int = Field(gt=0)
    equivalent_terminal_outcome_count: int = Field(gt=0)
    recent_tool_call_window: int = Field(gt=0)
    source_event_refs: tuple[ContextEventReferenceFact, ...]
    recurrence_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _recurrence(self) -> "RecentToolActionRecurrenceFact":
        if self.equivalent_terminal_outcome_count > self.action_occurrence_count:
            raise ValueError("equivalent outcomes exceed action occurrences")
        if self.action_occurrence_count > self.recent_tool_call_window:
            raise ValueError("action occurrences exceed recurrence window")
        if len(self.source_event_refs) > self.recent_tool_call_window:
            raise ValueError("recurrence source refs exceed bounded window")
        _validate_fingerprint(
            self,
            namespace="recent-tool-action-recurrence:v1",
            field_name="recurrence_fingerprint",
        )
        return self


class RolloutStatusShadowProjectionFact(FrozenLongHorizonFact):
    account_id: str = Field(min_length=1)
    source_through_sequence: int = Field(ge=0)
    settled_model_call_count: int = Field(ge=0)
    settled_tool_call_count: int = Field(ge=0)
    exploration_consumption_ratio_ppm: int = Field(ge=0)
    recurrence: tuple[RecentToolActionRecurrenceFact, ...]
    derivation_fingerprint: str = Field(min_length=1)
    model_visible: Literal[False] = False

    @model_validator(mode="after")
    def _shadow(self) -> "RolloutStatusShadowProjectionFact":
        _validate_fingerprint(
            self,
            namespace="rollout-status-shadow:v1",
            field_name="derivation_fingerprint",
        )
        return self


class LongHorizonRolloutStatusCandidateFact(FrozenLongHorizonFact):
    schema_version: Literal["long_horizon_rollout_status_candidate.v1"] = (
        "long_horizon_rollout_status_candidate.v1"
    )
    account_id: str = Field(min_length=1)
    rollout_phase: RolloutPhase
    settled_model_call_count: int = Field(ge=0)
    settled_tool_call_count: int = Field(ge=0)
    exploration_consumption_ratio_ppm: int = Field(ge=0)
    remaining_exploration_milliunits: int = Field(ge=0)
    finalization_reserve_milliunits: int = Field(gt=0)
    allowed_action_classes: tuple[LongHorizonActionClass, ...]
    recurrence: tuple[RecentToolActionRecurrenceFact, ...]
    source_event_refs: tuple[ContextEventReferenceFact, ...]
    semantic_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _candidate(self) -> "LongHorizonRolloutStatusCandidateFact":
        if len(self.allowed_action_classes) != len(set(self.allowed_action_classes)):
            raise ValueError("rollout status action classes must be unique")
        if tuple(sorted(self.allowed_action_classes, key=str)) != (
            self.allowed_action_classes
        ):
            raise ValueError("rollout status action classes must be sorted")
        if not self.source_event_refs:
            raise ValueError("rollout status candidate requires durable source refs")
        _validate_fingerprint(
            self,
            namespace="long-horizon-rollout-status-candidate:v1",
            field_name="semantic_fingerprint",
        )
        return self


class RolloutBudgetFeasibilityResult(FrozenLongHorizonFact):
    schema_version: Literal["rollout_budget_feasibility.v1"] = (
        "rollout_budget_feasibility.v1"
    )
    execution_profile_kind: Literal["host_root", "subagent_child"]
    execution_profile_id: str = Field(min_length=1)
    primary_target_slot: str = Field(min_length=1)
    primary_target_fingerprint: str = Field(min_length=1)
    summarizer_target_slot: str = Field(min_length=1)
    summarizer_target_fingerprint: str = Field(min_length=1)
    resolved_window_policy_fingerprint: str = Field(min_length=1)
    policy_fingerprint: str = Field(min_length=1)
    total_rollout_budget_milliunits: int = Field(gt=0)
    finalization_agent_reserve_milliunits: int = Field(gt=0)
    finalization_compaction_reserve_milliunits: int = Field(gt=0)
    finalization_tool_reserve_milliunits: int = Field(gt=0)
    finalization_reserve_milliunits: int = Field(gt=0)
    exploration_allowance_milliunits: int
    feasible: bool
    reason_code: Literal["feasible", "exploration_allowance_non_positive"]
    result_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _feasibility(self) -> "RolloutBudgetFeasibilityResult":
        reserve = (
            self.finalization_agent_reserve_milliunits
            + self.finalization_compaction_reserve_milliunits
            + self.finalization_tool_reserve_milliunits
        )
        if reserve != self.finalization_reserve_milliunits:
            raise ValueError("rollout feasibility reserve breakdown mismatch")
        if (
            self.total_rollout_budget_milliunits
            != reserve + self.exploration_allowance_milliunits
        ):
            raise ValueError("rollout feasibility total mismatch")
        if self.feasible != (self.exploration_allowance_milliunits > 0):
            raise ValueError("rollout feasibility boolean mismatch")
        if (self.reason_code == "feasible") != self.feasible:
            raise ValueError("rollout feasibility reason mismatch")
        _validate_fingerprint(
            self,
            namespace="rollout-budget-feasibility:v1",
            field_name="result_fingerprint",
        )
        return self


def calculate_model_call_reservation(
    *,
    target: ResolvedModelTargetFact,
    resolved_model_call_id: str | None,
    policy: RolloutBudgetPolicyFact,
) -> ModelCallReservationQuoteFact:
    semantic_payload = {
        "target_fingerprint": target.target_fingerprint,
        "physical_input_token_upper_bound": (
            target.context_budget.pre_margin_input_tokens
        ),
        "output_token_upper_bound": target.context_budget.effective_output_tokens,
        "non_cached_input_weight_milli": policy.non_cached_input_weight_milli,
        "output_weight_milli": policy.output_weight_milli,
        "policy_fingerprint": policy.policy_fingerprint,
    }
    semantic_fingerprint = context_fingerprint(
        "model-call-reservation-quote-semantic:v1", semantic_payload
    )
    fact_fingerprint = (
        None
        if resolved_model_call_id is None
        else context_fingerprint(
            "model-call-reservation-quote-fact:v1",
            {
                "resolved_model_call_id": resolved_model_call_id,
                "quote_semantic_fingerprint": semantic_fingerprint,
            },
        )
    )
    return ModelCallReservationQuoteFact(
        resolved_model_call_id=resolved_model_call_id,
        reserved_milliunits=(
            target.context_budget.pre_margin_input_tokens
            * policy.non_cached_input_weight_milli
            + target.context_budget.effective_output_tokens
            * policy.output_weight_milli
        ),
        quote_semantic_fingerprint=semantic_fingerprint,
        quote_fact_fingerprint=fact_fingerprint,
        **semantic_payload,
    )


def evaluate_rollout_budget_feasibility(
    *,
    execution_profile_kind: Literal["host_root", "subagent_child"],
    execution_profile_id: str,
    primary_target_slot: str,
    primary_target: ResolvedModelTargetFact,
    summarizer_target_slot: str,
    summarizer_target: ResolvedModelTargetFact,
    policy: RolloutBudgetPolicyFact,
) -> RolloutBudgetFeasibilityResult:
    """Evaluate one production target pair with the runtime reservation formula."""

    primary_quote = calculate_model_call_reservation(
        target=primary_target,
        resolved_model_call_id=None,
        policy=policy,
    )
    summarizer_quote = calculate_model_call_reservation(
        target=summarizer_target,
        resolved_model_call_id=None,
        policy=policy,
    )
    total = (
        primary_target.context_budget.input_budget_tokens
        * policy.total_input_budget_multiplier_milli
    )
    final_agent = (
        primary_quote.reserved_milliunits
        * policy.finalization_reserved_model_calls
    )
    final_compaction = (
        summarizer_quote.reserved_milliunits
        * policy.finalization_reserved_window_compactions
    )
    final_tool = (
        policy.finalization_reserved_tool_cost_units
        * policy.tool_cost_unit_weight_milli
    )
    reserve = final_agent + final_compaction + final_tool
    exploration = total - reserve
    feasible = exploration > 0
    payload = {
        "schema_version": "rollout_budget_feasibility.v1",
        "execution_profile_kind": execution_profile_kind,
        "execution_profile_id": execution_profile_id,
        "primary_target_slot": primary_target_slot,
        "primary_target_fingerprint": primary_target.target_fingerprint,
        "summarizer_target_slot": summarizer_target_slot,
        "summarizer_target_fingerprint": summarizer_target.target_fingerprint,
        "resolved_window_policy_fingerprint": default_long_horizon_context_policy(
            input_budget_tokens=primary_target.context_budget.input_budget_tokens
        ).policy_fingerprint,
        "policy_fingerprint": policy.policy_fingerprint,
        "total_rollout_budget_milliunits": total,
        "finalization_agent_reserve_milliunits": final_agent,
        "finalization_compaction_reserve_milliunits": final_compaction,
        "finalization_tool_reserve_milliunits": final_tool,
        "finalization_reserve_milliunits": reserve,
        "exploration_allowance_milliunits": exploration,
        "feasible": feasible,
        "reason_code": (
            "feasible" if feasible else "exploration_allowance_non_positive"
        ),
    }
    return RolloutBudgetFeasibilityResult(
        **payload,
        result_fingerprint=context_fingerprint(
            "rollout-budget-feasibility:v1", payload
        ),
    )


def default_long_horizon_context_policy(
    *, input_budget_tokens: int
) -> LongHorizonContextAllocationPolicyFact:
    payload = {
        "tool_projection_soft_ratio_ppm": 250_000,
        "tool_projection_post_rewrite_ratio_ppm": 180_000,
        "window_compaction_trigger_ratio_ppm": 800_000,
        "window_compaction_post_target_ratio_ppm": 550_000,
        "latest_tool_result_reserve_tokens": max(
            1, min(4_096, input_budget_tokens * 20_000 // 1_000_000)
        ),
        "current_run_recent_unit_count": 4,
        "max_projection_units_per_window": 256,
        "max_rollup_members": 64,
        "max_rewrite_entries_per_page": 128,
        "max_safe_point_revisions": 16,
        "max_compile_attempts_per_model_call": 4,
    }
    return LongHorizonContextAllocationPolicyFact(
        **payload,
        policy_fingerprint=context_fingerprint(
            "long-horizon-context-allocation:v1",
            {"schema_version": "long_horizon_context_allocation.v1", **payload},
        ),
    )


def default_rollout_budget_policy() -> RolloutBudgetPolicyFact:
    payload = {
        "total_input_budget_multiplier_milli": 8_000,
        "non_cached_input_weight_milli": 1_000,
        "cached_input_weight_milli": 100,
        "output_weight_milli": 4_000,
        "tool_cost_unit_weight_milli": 1_000_000,
        "finalization_reserved_model_calls": 2,
        "finalization_reserved_window_compactions": 1,
        "finalization_reserved_tool_cost_units": 16,
        "warning_consumption_ratio_ppm": 600_000,
        "restricted_consumption_ratio_ppm": 800_000,
        "finalization_consumption_ratio_ppm": 1_000_000,
        "emergency_model_call_limit": 200,
        "emergency_tool_call_limit": 256,
        "max_concurrent_subagent_reservations": 8,
    }
    return RolloutBudgetPolicyFact(
        **payload,
        policy_fingerprint=context_fingerprint(
            "rollout-budget-policy:v1",
            {"schema_version": "rollout_budget_policy.v1", **payload},
        ),
    )


def default_child_rollout_policy() -> ChildRolloutReservationPolicyFact:
    payload = {
        "max_agent_model_calls_per_child": 16,
        "max_window_compactions_per_child": 1,
        "max_tool_cost_units_per_child": 32,
        "max_parent_exploration_share_ppm": 500_000,
    }
    return ChildRolloutReservationPolicyFact(
        **payload,
        policy_fingerprint=context_fingerprint(
            "child-rollout-reservation-policy:v1",
            {"schema_version": "child_rollout_reservation_policy.v1", **payload},
        ),
    )


def default_rollout_status_hint_policy() -> RolloutStatusHintPolicyFact:
    payload = {
        "recent_tool_call_window": 16,
        "minimum_equivalent_outcome_occurrences": 3,
        "max_recurrence_entries": 4,
    }
    return RolloutStatusHintPolicyFact(
        **payload,
        policy_fingerprint=context_fingerprint(
            "rollout-status-hint-policy:v1",
            {"schema_version": "rollout-status-hint-policy:v1", **payload},
        ),
    )


def _validate_fingerprint(
    value: FrozenLongHorizonFact,
    *,
    namespace: str,
    field_name: str,
) -> None:
    payload = value.model_dump(mode="json", exclude={field_name})
    expected = context_fingerprint(namespace, payload)
    if getattr(value, field_name) != expected:
        raise ValueError(f"{field_name} mismatch")


def default_subagent_graph_checkpoint_policy() -> SubagentGraphCheckpointPolicyFact:
    payload = {
        "checkpoint_every_events": 512,
        "checkpoint_max_delta_events": 32_768,
        "checkpoint_max_delta_bytes": 33_554_432,
        "bootstrap_max_events": 2048,
        "bootstrap_max_bytes": 8_388_608,
        "rebase_max_checkpoint_candidates": 8,
        "retained_checkpoint_min_count": 2,
    }
    return SubagentGraphCheckpointPolicyFact(
        **payload,
        policy_fingerprint=context_fingerprint(
            "subagent-graph-checkpoint-policy:v1", payload
        ),
    )
