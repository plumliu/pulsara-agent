"""Frozen typed contracts for durable-runtime benchmark scenarios."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    model_validator,
)


PRODUCTION_SEMANTIC_BATCH_MAX_EVENTS = 16


class FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GeneratorContract(FrozenContract):
    generator_id: str = Field(min_length=1, max_length=128)
    generator_version: str = Field(min_length=1, max_length=32)


class GraderContract(FrozenContract):
    grader_id: str = Field(min_length=1, max_length=128)
    grader_version: str = Field(min_length=1, max_length=32)
    assertion_ids: tuple[str, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _unique_assertions(self) -> "GraderContract":
        if len(set(self.assertion_ids)) != len(self.assertion_ids):
            raise ValueError("grader assertion IDs must be unique")
        return self


class WriterMeasurementContract(FrozenContract):
    warmup_iterations: int = Field(ge=0, le=100)
    measured_iterations: int = Field(ge=1, le=100)
    reset_mode: Literal[
        "fresh_runtime_session_per_iteration",
        "fresh_session_set_per_iteration",
    ]


ContextMode = Literal[
    "process_cold",
    "steady_state",
    "verified_artifact_cache_warm",
]


class ContextMeasurementContract(FrozenContract):
    modes: tuple[ContextMode, ...] = Field(min_length=1, max_length=3)
    warmup_iterations: int = Field(ge=0, le=100)
    measured_iterations: int = Field(ge=1, le=100)

    @model_validator(mode="after")
    def _unique_modes(self) -> "ContextMeasurementContract":
        if len(set(self.modes)) != len(self.modes):
            raise ValueError("context measurement modes must be unique")
        return self


class DefaultContextMeasurementContract(FrozenContract):
    warmup_iterations: int = Field(ge=0, le=100)
    measured_iterations: int = Field(ge=1, le=100)


class ScenarioBase(FrozenContract):
    schema_version: str
    scenario_id: str
    seed: int = Field(gt=0)
    external_network_access: Literal["forbidden"]
    allowed_local_services: tuple[Literal["postgresql"], ...] = ("postgresql",)
    description: str = Field(min_length=1, max_length=1024)
    generator_contract: GeneratorContract
    grader_contract: GraderContract
    invariants: tuple[str, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _network_and_invariants(self) -> "ScenarioBase":
        if self.allowed_local_services != ("postgresql",):
            raise ValueError("offline scenarios may only allow local PostgreSQL")
        if any(not invariant.strip() for invariant in self.invariants):
            raise ValueError("scenario invariants must be non-empty")
        return self


class StreamBlockBase(FrozenContract):
    kind: str
    block_count: int = Field(ge=1, le=16_384)
    delta_events_per_block: int = Field(ge=1, le=16_384)
    characters_per_delta: int = Field(ge=1, le=1_048_576)


class TextStreamBlock(StreamBlockBase):
    kind: Literal["text"]


class ThinkingStreamBlock(StreamBlockBase):
    kind: Literal["thinking"]


class DataStreamBlock(StreamBlockBase):
    kind: Literal["data"]
    media_type: Literal["application/json"]


class ToolCallStreamBlock(StreamBlockBase):
    kind: Literal["tool_call"]
    tool_name_pattern: Literal["benchmark_tool_{ordinal}"]


MixedStreamBlock = Annotated[
    TextStreamBlock | ThinkingStreamBlock | DataStreamBlock | ToolCallStreamBlock,
    Field(discriminator="kind"),
]


class ModelStreamWorkload(FrozenContract):
    blocks: tuple[MixedStreamBlock, ...] = Field(min_length=1, max_length=128)
    terminal_outcome: Literal["completed"]
    usage_status: Literal["missing", "reported"]


class SingleModelCallWorkload(FrozenContract):
    runtime_sessions: Literal[1]
    runs_per_session: Literal[1]
    model_calls_per_run: Literal[1]
    model_stream: ModelStreamWorkload


CaseKind = Literal[
    "production_valid",
    "sensitivity_analysis",
    "counterfactual_analysis",
]


class SemanticBatchCase(FrozenContract):
    case_id: str = Field(min_length=1, max_length=128)
    case_kind: CaseKind
    max_business_events_per_commit: int = Field(ge=1, le=64)

    @model_validator(mode="after")
    def _production_bound(self) -> "SemanticBatchCase":
        if (
            self.max_business_events_per_commit
            > PRODUCTION_SEMANTIC_BATCH_MAX_EVENTS
            and self.case_kind != "counterfactual_analysis"
        ):
            raise ValueError(
                "over-bound semantic batches must be counterfactual"
            )
        return self


class ModelSemanticBatchMatrixScenario(ScenarioBase):
    schema_version: Literal["pulsara.durable-runtime.writer-scenario.v1"]
    scenario_id: Literal["model-semantic-batch-matrix"]
    workload: SingleModelCallWorkload
    execution_matrix: tuple[SemanticBatchCase, ...] = Field(
        min_length=1,
        max_length=16,
    )
    production_baseline_case_id: str = Field(min_length=1, max_length=128)
    semantic_reference_case_id: str = Field(min_length=1, max_length=128)
    measurement: WriterMeasurementContract

    @model_validator(mode="after")
    def _shape(self) -> "ModelSemanticBatchMatrixScenario":
        if len(self.workload.model_stream.blocks) != 1:
            raise ValueError("batch matrix requires one text block")
        block = self.workload.model_stream.blocks[0]
        if not isinstance(block, TextStreamBlock):
            raise ValueError("batch matrix requires one text block")
        if self.workload.model_stream.usage_status != "missing":
            raise ValueError("batch matrix freezes missing usage")
        _require_unique_case_ids(self.execution_matrix)
        if len(self.execution_matrix) != 1:
            raise ValueError("segment-v1 scenario has exactly one production case")
        only_case = self.execution_matrix[0]
        if (
            only_case.case_id != "segment-v1"
            or only_case.case_kind != "production_valid"
            or only_case.max_business_events_per_commit != 16
        ):
            raise ValueError("segment-v1 must use the production durable batch bound")
        reference = tuple(
            case
            for case in self.execution_matrix
            if case.case_id == self.semantic_reference_case_id
        )
        baseline = tuple(
            case
            for case in self.execution_matrix
            if case.case_id == self.production_baseline_case_id
        )
        if (
            len(baseline) != 1
            or baseline[0].case_kind != "production_valid"
            or baseline[0].case_id != "segment-v1"
        ):
            raise ValueError(
                "production baseline must identify the segment-v1 case"
            )
        if len(reference) != 1 or reference[0].case_kind != "production_valid":
            raise ValueError(
                "semantic reference must identify one production-valid case"
            )
        if self.semantic_reference_case_id != self.production_baseline_case_id:
            raise ValueError(
                "semantic reference must equal the production baseline case"
            )
        return self


StructuralPolicy = Literal[
    "flush_each_start_and_end",
    "group_start_with_following_delta_and_end_with_preceding_delta",
]


class StructuralGroupingCase(SemanticBatchCase):
    structural_policy: StructuralPolicy


class ModelSemanticStructuralGroupingScenario(ScenarioBase):
    schema_version: Literal["pulsara.durable-runtime.writer-scenario.v1"]
    scenario_id: Literal["model-semantic-structural-grouping"]
    workload: SingleModelCallWorkload
    execution_matrix: tuple[StructuralGroupingCase, ...] = Field(
        min_length=1,
        max_length=8,
    )
    measurement: WriterMeasurementContract

    @model_validator(mode="after")
    def _shape(self) -> "ModelSemanticStructuralGroupingScenario":
        kinds = tuple(block.kind for block in self.workload.model_stream.blocks)
        if kinds != ("thinking", "text", "data", "tool_call"):
            raise ValueError("structural grouping block matrix drifted")
        if self.workload.model_stream.usage_status != "reported":
            raise ValueError("structural grouping requires reported usage")
        _require_unique_case_ids(self.execution_matrix)
        return self


class ContentionModelStream(FrozenContract):
    blocks_per_call: Literal[1]
    kind: Literal["text"]
    delta_events_per_block: int = Field(ge=1, le=16_384)
    characters_per_delta: int = Field(ge=1, le=1_048_576)
    max_business_events_per_commit: int = Field(
        ge=1,
        le=PRODUCTION_SEMANTIC_BATCH_MAX_EVENTS,
    )
    terminal_outcome: Literal["completed"]
    usage_status: Literal["missing"]


class MultiSessionWorkload(FrozenContract):
    runtime_sessions: int = Field(ge=2, le=64)
    runs_per_session: Literal[1]
    model_calls_per_run: int = Field(ge=1, le=128)
    model_stream: ContentionModelStream


class MultiSessionCase(FrozenContract):
    case_id: str = Field(min_length=1, max_length=128)
    case_kind: Literal["production_valid"]
    concurrent_sessions: int = Field(ge=1, le=64)


class MultiSessionContentionScenario(ScenarioBase):
    schema_version: Literal["pulsara.durable-runtime.writer-scenario.v1"]
    scenario_id: Literal["multi-session-contention"]
    workload: MultiSessionWorkload
    execution_matrix: tuple[MultiSessionCase, ...] = Field(
        min_length=1,
        max_length=16,
    )
    measurement: WriterMeasurementContract

    @model_validator(mode="after")
    def _shape(self) -> "MultiSessionContentionScenario":
        _require_unique_case_ids(self.execution_matrix)
        concurrency = tuple(case.concurrent_sessions for case in self.execution_matrix)
        if concurrency != tuple(sorted(set(concurrency))):
            raise ValueError("contention concurrency levels must be sorted and unique")
        if concurrency[-1] != self.workload.runtime_sessions:
            raise ValueError("contention matrix must reach the full session count")
        if any(value > self.workload.runtime_sessions for value in concurrency):
            raise ValueError("contention case exceeds generated session count")
        if self.measurement.reset_mode != "fresh_session_set_per_iteration":
            raise ValueError("contention requires a fresh session set per iteration")
        return self


class StableConfirmationWorkload(FrozenContract):
    runtime_sessions: Literal[1]
    runs_per_session: Literal[1]
    model_calls_per_run: Literal[1]
    semantic_business_events: int = Field(ge=1, le=1_000_000)
    characters_per_delta: int = Field(ge=1, le=1_048_576)
    max_business_events_per_commit: int = Field(
        ge=1,
        le=PRODUCTION_SEMANTIC_BATCH_MAX_EVENTS,
    )


class NoFault(FrozenContract):
    kind: Literal["none"]


class PeriodicRetryFault(FrozenContract):
    kind: Literal["fail_before_commit", "commit_then_raise"]
    every_nth_batch: int = Field(ge=1)
    max_failures_per_batch: Literal[1]


class CallerCancellationFault(FrozenContract):
    kind: Literal["caller_cancel_after_physical_start"]
    every_nth_batch: int = Field(ge=1)


class ConfirmationUnknownFault(FrozenContract):
    kind: Literal["confirmation_unknown"]
    batch_ordinals: tuple[int, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _ordinals(self) -> "ConfirmationUnknownFault":
        if tuple(sorted(set(self.batch_ordinals))) != self.batch_ordinals:
            raise ValueError("fault batch ordinals must be sorted and unique")
        if self.batch_ordinals[0] < 1:
            raise ValueError("fault batch ordinals are one-based")
        return self


StableFault = Annotated[
    NoFault | PeriodicRetryFault | CallerCancellationFault | ConfirmationUnknownFault,
    Field(discriminator="kind"),
]


class StableConfirmationCase(FrozenContract):
    case_id: str = Field(min_length=1, max_length=128)
    case_kind: Literal["production_valid"]
    fault: StableFault


class StableConfirmationFaultsScenario(ScenarioBase):
    schema_version: Literal["pulsara.durable-runtime.writer-scenario.v1"]
    scenario_id: Literal["stable-confirmation-faults"]
    workload: StableConfirmationWorkload
    execution_matrix: tuple[StableConfirmationCase, ...] = Field(
        min_length=1,
        max_length=16,
    )
    measurement: WriterMeasurementContract

    @model_validator(mode="after")
    def _shape(self) -> "StableConfirmationFaultsScenario":
        _require_unique_case_ids(self.execution_matrix)
        batch_count = (
            self.workload.semantic_business_events
            + self.workload.max_business_events_per_commit
            - 1
        ) // self.workload.max_business_events_per_commit
        for case in self.execution_matrix:
            fault = case.fault
            if isinstance(fault, ConfirmationUnknownFault) and (
                fault.batch_ordinals[-1] > batch_count
            ):
                raise ValueError("confirmation fault exceeds generated batch count")
            if isinstance(fault, (PeriodicRetryFault, CallerCancellationFault)) and (
                fault.every_nth_batch > batch_count
            ):
                raise ValueError("periodic fault never triggers")
        return self


class MixedRuntimePerCycle(FrozenContract):
    model_calls: Literal[1]
    model_semantic_delta_events: int = Field(ge=1, le=16_384)
    model_semantic_characters_per_delta: int = Field(ge=1, le=1_048_576)
    accepted_control_dispositions: Literal[1]
    tool_calls: int = Field(ge=1, le=128)
    tool_result_payload_characters: int = Field(ge=1, le=16_777_216)
    subagent_graph_events: int = Field(ge=1, le=1_024)
    context_audit_events: int = Field(ge=1, le=1_024)


class MixedRuntimeWorkload(FrozenContract):
    runtime_sessions: Literal[1]
    runs_per_session: Literal[1]
    cycles: int = Field(ge=1, le=1_024)
    per_cycle: MixedRuntimePerCycle
    checkpoint_after_cycles: tuple[int, ...] = Field(min_length=1, max_length=128)
    terminal_status: Literal["finished"]

    @model_validator(mode="after")
    def _checkpoints(self) -> "MixedRuntimeWorkload":
        if tuple(sorted(set(self.checkpoint_after_cycles))) != (
            self.checkpoint_after_cycles
        ):
            raise ValueError("checkpoint cycle ordinals must be sorted and unique")
        if self.checkpoint_after_cycles[-1] != self.cycles:
            raise ValueError("the final cycle must end at a checkpoint")
        if self.checkpoint_after_cycles[0] < 1:
            raise ValueError("checkpoint cycle ordinals are one-based")
        return self


class MixedRuntimeCase(FrozenContract):
    case_id: Literal["production-shape"]
    case_kind: Literal["production_valid"]
    model_semantic_batch_events: int = Field(
        ge=1,
        le=PRODUCTION_SEMANTIC_BATCH_MAX_EVENTS,
    )
    tool_result_batching: Literal["production"]
    checkpoint_policy: Literal["pairing_safe"]


class MixedRuntimeAccountingScenario(ScenarioBase):
    schema_version: Literal["pulsara.durable-runtime.writer-scenario.v1"]
    scenario_id: Literal["mixed-runtime-accounting"]
    workload: MixedRuntimeWorkload
    execution_matrix: tuple[MixedRuntimeCase, ...] = Field(min_length=1, max_length=1)
    measurement: WriterMeasurementContract


class ToolResultProfile(FrozenContract):
    default_characters: int = Field(ge=1, le=16_777_216)
    large_every_nth_result: int = Field(ge=1, le=1_024)
    large_characters: int = Field(ge=1, le=16_777_216)
    artifact_backed: Literal[True]


class LongPlanLedger(FrozenContract):
    runtime_sessions: Literal[1]
    runs: Literal[1]
    prior_transcript_messages: int = Field(ge=0, le=10_000)
    model_calls: int = Field(ge=1, le=1_024)
    semantic_delta_events_per_call: tuple[int, ...] = Field(
        min_length=1,
        max_length=1_024,
    )
    characters_per_semantic_delta: int = Field(ge=1, le=1_048_576)
    tool_calls_per_model_call: tuple[int, ...] = Field(
        min_length=1,
        max_length=1_024,
    )
    tool_result_profile: ToolResultProfile
    checkpoint_policy: Literal["do_not_advance_during_measurement"]

    @model_validator(mode="after")
    def _call_vectors(self) -> "LongPlanLedger":
        if len(self.semantic_delta_events_per_call) != self.model_calls:
            raise ValueError("semantic delta vector must match model call count")
        if len(self.tool_calls_per_model_call) != self.model_calls:
            raise ValueError("tool call vector must match model call count")
        if any(value < 0 for value in self.tool_calls_per_model_call):
            raise ValueError("tool call counts cannot be negative")
        if sum(self.semantic_delta_events_per_call) != 4_096:
            raise ValueError("long-plan fixture must contain 4096 semantic deltas")
        if sum(self.tool_calls_per_model_call) != 21:
            raise ValueError("long-plan fixture must contain 21 tool calls")
        return self


class OrdinalCompileSchedule(FrozenContract):
    after_model_call_ordinals: tuple[int, ...] = Field(min_length=1, max_length=1_024)
    compile_attempts_per_point: Literal[1]


class LongPlanPrefixGrowthScenario(ScenarioBase):
    schema_version: Literal["pulsara.durable-runtime.context-scenario.v1"]
    scenario_id: Literal["long-plan-prefix-growth"]
    ledger: LongPlanLedger
    compile_schedule: OrdinalCompileSchedule
    measurement: ContextMeasurementContract

    @model_validator(mode="after")
    def _schedule(self) -> "LongPlanPrefixGrowthScenario":
        if self.compile_schedule.after_model_call_ordinals != tuple(
            range(1, self.ledger.model_calls + 1)
        ):
            raise ValueError("long-plan compile schedule must cover every call")
        if self.measurement.modes != ("process_cold", "steady_state"):
            raise ValueError("long-plan mode contract drifted")
        return self


class IncrementalWindowLedger(FrozenContract):
    runtime_sessions: Literal[1]
    runs: Literal[1]
    prior_transcript_messages: int = Field(ge=0, le=10_000)
    model_calls: int = Field(ge=1, le=1_024)
    semantic_delta_events_per_call: int = Field(ge=1, le=16_384)
    characters_per_semantic_delta: int = Field(ge=1, le=1_048_576)
    tool_calls_every_nth_model_call: int = Field(ge=1, le=1_024)
    tool_result_characters: int = Field(ge=1, le=16_777_216)
    checkpoint_policy: Literal["do_not_advance_during_measurement"]

    @model_validator(mode="after")
    def _tool_frequency(self) -> "IncrementalWindowLedger":
        if self.tool_calls_every_nth_model_call > self.model_calls:
            raise ValueError("tool frequency exceeds model call count")
        if self.semantic_delta_events_per_call != 128:
            raise ValueError(
                "incremental window fixture must contain 128 deltas per call"
            )
        return self


class EveryCallCompileSchedule(FrozenContract):
    after_every_model_call: Literal[True]
    compile_attempts_per_point: Literal[1]


class IncrementalActiveWindowScenario(ScenarioBase):
    schema_version: Literal["pulsara.durable-runtime.context-scenario.v1"]
    scenario_id: Literal["incremental-active-window"]
    ledger: IncrementalWindowLedger
    compile_schedule: EveryCallCompileSchedule
    measurement: ContextMeasurementContract

    @model_validator(mode="after")
    def _modes(self) -> "IncrementalActiveWindowScenario":
        if self.measurement.modes != ("process_cold", "steady_state"):
            raise ValueError("incremental window mode contract drifted")
        return self


class WindowCompactionFixture(FrozenContract):
    summary_characters: int = Field(ge=1, le=16_777_216)
    retained_recent_messages: int = Field(ge=0, le=10_000)
    source_document_artifact: Literal[True]
    completed_terminal: Literal[True]


class SingleCompactionLedger(FrozenContract):
    runtime_sessions: Literal[1]
    runs: Literal[1]
    prior_tool_observations: int = Field(ge=1, le=10_000)
    prior_tool_observation_characters: int = Field(ge=1, le=16_777_216)
    model_calls_before_compaction: int = Field(ge=2, le=1_024)
    semantic_delta_events_before_compaction: int = Field(ge=1, le=16_384)
    characters_per_semantic_delta: int = Field(ge=1, le=1_048_576)
    window_compaction: WindowCompactionFixture
    post_compaction_model_calls: int = Field(ge=1, le=1_024)
    post_compaction_semantic_delta_events_per_call: int = Field(ge=1, le=16_384)

    @model_validator(mode="after")
    def _pairing_groups(self) -> "SingleCompactionLedger":
        if self.model_calls_before_compaction != self.prior_tool_observations:
            raise ValueError(
                "single compaction fixture requires one tool pair per model call"
            )
        return self


class SingleCompactionCompileSchedule(FrozenContract):
    phases: tuple[
        Literal[
            "before_compaction",
            "immediately_after_compaction",
            "after_first_post_compaction_call",
            "after_second_post_compaction_call",
        ],
        ...,
    ] = Field(min_length=4, max_length=4)


class SingleLongCompactionScenario(ScenarioBase):
    schema_version: Literal["pulsara.durable-runtime.context-scenario.v1"]
    scenario_id: Literal["single-long-compaction"]
    ledger: SingleCompactionLedger
    compile_schedule: SingleCompactionCompileSchedule
    measurement: ContextMeasurementContract

    @model_validator(mode="after")
    def _shape(self) -> "SingleLongCompactionScenario":
        expected = (
            "before_compaction",
            "immediately_after_compaction",
            "after_first_post_compaction_call",
            "after_second_post_compaction_call",
        )
        if self.compile_schedule.phases != expected:
            raise ValueError("single compaction phases drifted")
        if self.ledger.post_compaction_model_calls != 2:
            raise ValueError("single compaction fixture requires two post calls")
        if self.measurement.modes != ("process_cold", "steady_state"):
            raise ValueError("single compaction mode contract drifted")
        return self


class ParentLedger(FrozenContract):
    model_calls: int = Field(ge=1, le=1_024)
    semantic_delta_events_per_call: int = Field(ge=1, le=16_384)
    characters_per_semantic_delta: int = Field(ge=1, le=1_048_576)
    tool_calls: int = Field(ge=0, le=1_024)
    compile_after_every_model_call: Literal[True]


class ChildLedger(FrozenContract):
    child_id: str = Field(min_length=1, max_length=128)
    depends_on: tuple[str, ...] = ()
    model_calls: int = Field(ge=1, le=1_024)
    target_raw_events: int = Field(ge=1, le=1_000_000)
    tool_calls: int = Field(ge=0, le=1_024)
    result_artifacts: int = Field(ge=1, le=64)
    handoff: Literal["explicit"]

    @model_validator(mode="after")
    def _explicit_report_shape(self) -> "ChildLedger":
        if self.model_calls != 1 or self.tool_calls != 1:
            raise ValueError(
                "context benchmark children use one large stream and one explicit report"
            )
        if self.target_raw_events <= 6:
            raise ValueError("child raw-event target must leave room for deltas")
        return self


class SubagentGraphCheckpointFixture(FrozenContract):
    enabled: Literal[True]
    checkpoint_after_terminal_children: Literal[True]


class SubagentLedger(FrozenContract):
    parent: ParentLedger
    children: tuple[ChildLedger, ...] = Field(min_length=2, max_length=64)
    subagent_graph_checkpoint: SubagentGraphCheckpointFixture

    @model_validator(mode="after")
    def _dependency_order(self) -> "SubagentLedger":
        ids = tuple(child.child_id for child in self.children)
        if len(set(ids)) != len(ids):
            raise ValueError("child IDs must be unique")
        seen: set[str] = set()
        for child in self.children:
            if any(dependency not in seen for dependency in child.depends_on):
                raise ValueError("child dependencies must reference an earlier child")
            seen.add(child.child_id)
        return self


class SubagentCompileSchedule(FrozenContract):
    parent_after_model_call_ordinals: tuple[int, ...] = Field(
        min_length=1,
        max_length=1_024,
    )
    include_points_after_child_results: Literal[True]


class SubagentTwoChildrenScenario(ScenarioBase):
    schema_version: Literal["pulsara.durable-runtime.context-scenario.v1"]
    scenario_id: Literal["subagent-two-children"]
    ledger: SubagentLedger
    compile_schedule: SubagentCompileSchedule
    measurement: ContextMeasurementContract

    @model_validator(mode="after")
    def _shape(self) -> "SubagentTwoChildrenScenario":
        if self.compile_schedule.parent_after_model_call_ordinals != tuple(
            range(1, self.ledger.parent.model_calls + 1)
        ):
            raise ValueError("parent compile schedule must cover every call")
        if self.measurement.modes != ("process_cold", "steady_state"):
            raise ValueError("subagent mode contract drifted")
        return self


class ArtifactToolResults(FrozenContract):
    total_results: int = Field(ge=1, le=100_000)
    canonical_result_characters: int = Field(ge=1, le=16_777_216)
    inline_envelope_characters: int = Field(ge=1, le=16_777_216)
    artifact_backed: Literal[True]
    media_type: Literal["text/plain"]
    content_pattern: Literal["unique-sentinel-plus-padding-v1"]

    @model_validator(mode="after")
    def _envelope_bound(self) -> "ArtifactToolResults":
        if self.inline_envelope_characters >= self.canonical_result_characters:
            raise ValueError("artifact envelope must be smaller than canonical content")
        return self


class ArtifactHeavyLedger(FrozenContract):
    runtime_sessions: Literal[1]
    runs: Literal[1]
    model_calls: int = Field(ge=1, le=1_024)
    semantic_delta_events_per_call: int = Field(ge=1, le=16_384)
    characters_per_semantic_delta: int = Field(ge=1, le=1_048_576)
    tool_calls_per_model_call: int = Field(ge=1, le=1_024)
    tool_results: ArtifactToolResults
    checkpoint_policy: Literal["pairing_safe_when_required"]

    @model_validator(mode="after")
    def _result_count(self) -> "ArtifactHeavyLedger":
        expected = self.model_calls * self.tool_calls_per_model_call
        if self.tool_results.total_results != expected:
            raise ValueError("artifact result total must match generated tool calls")
        return self


class RepeatedOrdinalCompileSchedule(FrozenContract):
    after_model_call_ordinals: tuple[int, ...] = Field(min_length=1, max_length=1_024)
    repeat_last_compile: int = Field(ge=1, le=1_024)


class ArtifactHeavyToolsScenario(ScenarioBase):
    schema_version: Literal["pulsara.durable-runtime.context-scenario.v1"]
    scenario_id: Literal["artifact-heavy-tools"]
    ledger: ArtifactHeavyLedger
    compile_schedule: RepeatedOrdinalCompileSchedule
    measurement: ContextMeasurementContract

    @model_validator(mode="after")
    def _shape(self) -> "ArtifactHeavyToolsScenario":
        if self.compile_schedule.after_model_call_ordinals != tuple(
            range(1, self.ledger.model_calls + 1)
        ):
            raise ValueError("artifact compile schedule must cover every call")
        if self.measurement.modes != (
            "process_cold",
            "steady_state",
            "verified_artifact_cache_warm",
        ):
            raise ValueError("artifact cache mode contract drifted")
        return self


class TranscriptCheckpointFixture(FrozenContract):
    checkpoint_id: str = Field(min_length=1, max_length=128)
    logical_semantic_high_water: int = Field(ge=1, le=1_000_000)
    artifact_state: Literal["available"]
    preferred: bool = False


class RestartFixture(FrozenContract):
    close_after_logical_semantic_high_water: int = Field(ge=1, le=1_000_000)
    reopen_same_runtime_session: Literal[True]
    post_reopen_model_calls: int = Field(ge=1, le=1_024)
    post_reopen_semantic_delta_events_per_call: int = Field(ge=1, le=16_384)


class CheckpointRestartLedger(FrozenContract):
    runtime_sessions: Literal[1]
    runs: Literal[1]
    semantic_events_before_first_checkpoint: int = Field(ge=1, le=1_000_000)
    semantic_events_before_preferred_checkpoint: int = Field(ge=1, le=1_000_000)
    semantic_events_before_close: int = Field(ge=1, le=1_000_000)
    characters_per_semantic_delta: int = Field(ge=1, le=1_048_576)
    checkpoints: tuple[TranscriptCheckpointFixture, ...] = Field(
        min_length=2,
        max_length=64,
    )
    restart: RestartFixture

    @model_validator(mode="after")
    def _checkpoint_order(self) -> "CheckpointRestartLedger":
        high_waters = tuple(
            checkpoint.logical_semantic_high_water for checkpoint in self.checkpoints
        )
        if high_waters != tuple(sorted(set(high_waters))):
            raise ValueError("checkpoint high-waters must be sorted and unique")
        preferred = tuple(
            checkpoint for checkpoint in self.checkpoints if checkpoint.preferred
        )
        if len(preferred) != 1:
            raise ValueError("checkpoint fixture requires one preferred checkpoint")
        if high_waters[0] != self.semantic_events_before_first_checkpoint:
            raise ValueError("first checkpoint high-water drifted")
        if (
            preferred[0].logical_semantic_high_water
            != self.semantic_events_before_preferred_checkpoint
        ):
            raise ValueError("preferred checkpoint high-water drifted")
        if self.semantic_events_before_close <= high_waters[-1]:
            raise ValueError("close high-water must follow all checkpoints")
        if (
            self.restart.close_after_logical_semantic_high_water
            != self.semantic_events_before_close
        ):
            raise ValueError("restart close high-water drifted")
        return self


class PreferredCheckpointCase(FrozenContract):
    case_id: Literal["preferred-hit"]
    case_kind: Literal["production_valid"]
    preferred_checkpoint_artifact_state: Literal["available"]


class MissingCheckpointRebaseCase(FrozenContract):
    case_id: Literal["preferred-missing-rebase"]
    case_kind: Literal["production_valid"]
    preferred_checkpoint_artifact_state: Literal["missing"]
    expected_rebase_checkpoint_id: str = Field(min_length=1, max_length=128)


class ColdNoCacheCase(FrozenContract):
    case_id: Literal["process-cold-no-cache"]
    case_kind: Literal["production_valid"]
    preferred_checkpoint_artifact_state: Literal["available"]
    drop_all_process_local_caches_before_reopen: Literal[True]


CheckpointRestartCase = Annotated[
    PreferredCheckpointCase | MissingCheckpointRebaseCase | ColdNoCacheCase,
    Field(discriminator="case_id"),
]


class RestartCompileSchedule(FrozenContract):
    phases: tuple[str, ...] = Field(min_length=1, max_length=1_024)


class CheckpointRebaseRestartScenario(ScenarioBase):
    schema_version: Literal["pulsara.durable-runtime.context-scenario.v1"]
    scenario_id: Literal["checkpoint-rebase-and-restart"]
    ledger: CheckpointRestartLedger
    execution_matrix: tuple[CheckpointRestartCase, ...] = Field(
        min_length=3,
        max_length=3,
    )
    compile_schedule: RestartCompileSchedule
    measurement: DefaultContextMeasurementContract

    @model_validator(mode="after")
    def _shape(self) -> "CheckpointRebaseRestartScenario":
        _require_unique_case_ids(self.execution_matrix)
        expected_phases = ("immediately_after_reopen",) + tuple(
            f"after_post_reopen_model_call_{ordinal}"
            for ordinal in range(1, self.ledger.restart.post_reopen_model_calls + 1)
        )
        if self.compile_schedule.phases != expected_phases:
            raise ValueError("restart compile phases drifted")
        rebase = next(
            case
            for case in self.execution_matrix
            if isinstance(case, MissingCheckpointRebaseCase)
        )
        checkpoint_ids = {
            checkpoint.checkpoint_id for checkpoint in self.ledger.checkpoints
        }
        if rebase.expected_rebase_checkpoint_id not in checkpoint_ids:
            raise ValueError("rebase target is not a generated checkpoint")
        return self


WriterScenarioContract = Annotated[
    ModelSemanticBatchMatrixScenario
    | ModelSemanticStructuralGroupingScenario
    | MultiSessionContentionScenario
    | StableConfirmationFaultsScenario
    | MixedRuntimeAccountingScenario,
    Field(discriminator="scenario_id"),
]

ContextScenarioContract = Annotated[
    LongPlanPrefixGrowthScenario
    | IncrementalActiveWindowScenario
    | SingleLongCompactionScenario
    | SubagentTwoChildrenScenario
    | ArtifactHeavyToolsScenario
    | CheckpointRebaseRestartScenario,
    Field(discriminator="scenario_id"),
]

ScenarioContract = WriterScenarioContract | ContextScenarioContract

WRITER_SCENARIO_ADAPTER = TypeAdapter(WriterScenarioContract)
CONTEXT_SCENARIO_ADAPTER = TypeAdapter(ContextScenarioContract)


def execution_cases(
    scenario: ScenarioContract,
) -> tuple[FrozenContract, ...]:
    matrix = getattr(scenario, "execution_matrix", None)
    if matrix is None:
        return (
            DefaultExecutionCase(
                case_id="default",
                case_kind="production_valid",
            ),
        )
    return tuple(matrix)


class DefaultExecutionCase(FrozenContract):
    case_id: Literal["default"]
    case_kind: Literal["production_valid"]


def measurement_modes(scenario: ScenarioContract) -> tuple[str, ...]:
    modes = getattr(scenario.measurement, "modes", None)
    return ("default",) if modes is None else tuple(modes)


def _require_unique_case_ids(cases: tuple[object, ...]) -> None:
    case_ids = tuple(getattr(case, "case_id") for case in cases)
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("execution case IDs must be unique")
