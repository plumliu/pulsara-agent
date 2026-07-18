"""Production-path generators for deterministic context preparation benchmarks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
from time import monotonic, perf_counter
from typing import AsyncIterator

from pulsara_agent.capability.result_semantics import (
    build_unknown_result_semantics,
)
from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.event import (
    ContextWindowCompactionCompletedEvent,
    ContextWindowCompactionStartedEvent,
    EventContext,
    RunEndEvent,
    SubagentRunCompletedEvent,
    SubagentRunStartedEvent,
)
from pulsara_agent.event_log import PostgresEventLog
from pulsara_agent.llm.commit import RuntimeSessionModelStreamEventCommitPort
from pulsara_agent.llm.config import (
    LLMConfig,
    ModelSlotConfig,
)
from pulsara_agent.llm.control import RunModelCallControlOwner
from pulsara_agent.llm.input import LLMMessage, ToolSpec
from pulsara_agent.llm.lifecycle import prepare_model_lifecycle_start_bundle
from pulsara_agent.llm.models import ModelRole
from pulsara_agent.llm.provider import ProviderProfile
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.llm.resolution import ResolvedModelCall, ResolvedModelTarget
from pulsara_agent.llm.runtime import LLMRuntime
from pulsara_agent.llm.raw_provider import RawLLMTransport, RawProviderStreamItem
from pulsara_agent.llm.sanitizing_transport import SanitizingLLMTransport
from pulsara_agent.memory.artifacts.postgres_archive import PostgresArtifactStore
from pulsara_agent.message import ToolResultState
from pulsara_agent.primitives.context import (
    canonical_json_bytes,
    context_fingerprint,
)
from pulsara_agent.primitives.transcript_projection import (
    CheckpointProjectionBaseFact,
)
from pulsara_agent.primitives.model_call import (
    ModelCallPurpose,
    ModelContextLimits,
    ModelContextMode,
    ModelTokenUsageFact,
    ResolvedModelCallFact,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.tool_result import ToolResultStateFact
from pulsara_agent.primitives.long_horizon import (
    calculate_model_call_reservation,
)
from pulsara_agent.primitives.terminal_projection import (
    ModelCallSemanticSourceFact,
    TerminalArtifactContentReferenceFact,
    ToolTerminalProjectionPayloadFact,
)
from pulsara_agent.runtime.context_input.candidate import (
    ContextCandidateCollectionInput,
)
from pulsara_agent.runtime.agent import AgentRuntime
from pulsara_agent.runtime.context_input.compiler import (
    compile_context_from_facts,
    provider_neutral_payload_fingerprint,
)
from pulsara_agent.runtime.context_input.live import (
    PreparedLiveContextSnapshot,
    prepare_live_context_snapshot,
    prepare_live_transcript_projection,
)
from pulsara_agent.runtime.context_input.render import (
    PreparedToolResultRenderOutput,
    render_prepared_tool_result_units,
)
from pulsara_agent.runtime.context_engine.types import CompiledContext
from pulsara_agent.runtime.authority_materialization.transcript_reducer import (
    stable_entry_projection_references,
)
from pulsara_agent.runtime.authority_materialization.checkpoint import (
    CommittedTranscriptCheckpoint,
)
from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.runtime.permission import preset_to_policy
from pulsara_agent.runtime.state import LoopBudget
from pulsara_agent.runtime.long_horizon.projection import (
    plan_new_result_ingest,
    prepare_current_run_projection_planning_input,
)
from pulsara_agent.runtime.long_horizon.window_compaction_service import (
    ContextWindowCompactionService,
    WindowCompactionRequest,
)
from pulsara_agent.runtime.long_horizon.accounting import (
    resolve_run_rollout_binding,
)
from pulsara_agent.runtime.long_horizon.coordinator import (
    build_rollout_phase_transition_event,
    plan_root_model_admission,
)
from pulsara_agent.runtime.tool_artifacts import (
    PostgresToolResultArtifactIndex,
)
from pulsara_agent.runtime.tool_loop import build_tool_result_error_events

from generators.runtime_fixture import (
    BenchmarkContextRun,
    bootstrap_benchmark_context_run,
    rebind_benchmark_context_run,
)
from generators.raw_provider_fixture import (
    text_delta as RawProviderTextDelta,
    text_end as RawProviderTextBlockEnd,
    text_start as RawProviderTextBlockStart,
    thinking_delta as RawProviderThinkingDelta,
    thinking_end as RawProviderThinkingBlockEnd,
    thinking_start as RawProviderThinkingBlockStart,
    tool_call_delta as RawProviderToolCallDelta,
    tool_call_end as RawProviderToolCallEnd,
    tool_call_start as RawProviderToolCallStart,
)
from scenario_contracts import (
    ArtifactHeavyToolsScenario,
    CheckpointRebaseRestartScenario,
    CheckpointRestartCase,
    ColdNoCacheCase,
    IncrementalActiveWindowScenario,
    LongPlanPrefixGrowthScenario,
    MissingCheckpointRebaseCase,
    PreferredCheckpointCase,
    SingleLongCompactionScenario,
    SubagentTwoChildrenScenario,
)


SupportedContextScenario = (
    LongPlanPrefixGrowthScenario
    | IncrementalActiveWindowScenario
    | ArtifactHeavyToolsScenario
)


@dataclass(frozen=True, slots=True)
class ContextCompilePointObservation:
    point_id: str
    context_prepare_wall_seconds: float
    context_compile_wall_seconds: float
    source_through_sequence: int
    authority_event_count: int
    semantic_delta_event_count: int
    stable_entry_count: int
    normalized_message_count: int
    normalized_tool_result_count: int
    projection_base_fingerprint: str
    projection_base_kind: str
    projection_base_id: str | None
    active_window_id: str
    active_window_generation: int
    source_summary_artifact_id: str | None
    semantic_source_fingerprint: str
    authority_plan_fingerprint: str
    normalized_transcript_fingerprint: str
    provider_payload_fingerprint: str
    terminal_document_count: int
    terminal_projection_source_delta_count: int
    artifact_backed_terminal_content_count: int
    max_stable_entry_bytes: int
    selected_subagent_result_count: int
    subagent_graph_semantic_fingerprint: str


@dataclass(frozen=True, slots=True)
class ContextPreparationObservation:
    scenario_id: str
    mode: str
    compile_points: tuple[ContextCompilePointObservation, ...]
    generated_semantic_delta_count: int
    generated_tool_result_count: int
    repeated_final_compile_semantics_equal: bool
    generated_checkpoint_ids: tuple[str, ...] = ()
    deleted_checkpoint_id: str | None = None
    expected_selected_checkpoint_id: str | None = None
    reopen_provider_semantics_equal: bool | None = None
    compaction_status: str | None = None
    compaction_source_artifact_verified: bool | None = None
    child_runtime_session_ids: tuple[str, ...] = ()
    child_terminal_event_ids: tuple[str, ...] = ()
    child_result_ids: tuple[str, ...] = ()
    subagent_graph_checkpoint_id: str | None = None
    child_ledger_identity_isolated: bool | None = None
    child_terminal_references_exact: bool | None = None
    child_dependency_order_valid: bool | None = None


@dataclass(frozen=True, slots=True)
class _PreparedCompilePoint:
    observation: ContextCompilePointObservation
    resolved_call: ResolvedModelCall
    prepared: PreparedLiveContextSnapshot
    rendered: PreparedToolResultRenderOutput
    compiled: CompiledContext


@dataclass(frozen=True, slots=True)
class _ChildScriptSpec:
    child_id: str
    objective_marker: str
    model_calls: int
    target_raw_events: int


@dataclass(frozen=True, slots=True)
class _TrajectoryStep:
    semantic_delta_events: int
    characters_per_delta: int
    tool_result_characters: tuple[int, ...]


class DeterministicContextStreamTransport(RawLLMTransport):
    api = "mock"
    binding_id = "pulsara.benchmark.deterministic-context-stream"
    contract_version = "v1"

    def __init__(
        self,
        *,
        call_ordinal: int,
        semantic_delta_events: int,
        characters_per_delta: int,
        tool_call_count: int,
    ) -> None:
        self._call_ordinal = call_ordinal
        self._semantic_delta_events = semantic_delta_events
        self._characters_per_delta = characters_per_delta
        self._tool_call_count = tool_call_count

    async def stream(
        self,
        *,
        call: ResolvedModelCall,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[RawProviderStreamItem | TransportUsageReport]:
        del call, context
        common = {
            **event_context.event_fields(),
            "created_at": "2026-01-01T00:00:00.000000Z",
        }
        block_id = f"benchmark-text:{self._call_ordinal}"
        yield RawProviderTextBlockStart(
            id=f"raw-text-start:{self._call_ordinal}",
            **common,
            block_id=block_id,
        )
        for index in range(self._semantic_delta_events):
            yield RawProviderTextDelta(
                id=f"raw-text-delta:{self._call_ordinal}:{index}",
                **common,
                block_id=block_id,
                delta=_fixed_width_ascii(
                    self._call_ordinal,
                    index,
                    self._characters_per_delta,
                ),
            )
        yield RawProviderTextBlockEnd(
            id=f"raw-text-end:{self._call_ordinal}",
            **common,
            block_id=block_id,
        )
        for tool_ordinal in range(1, self._tool_call_count + 1):
            tool_call_id = _tool_call_id(self._call_ordinal, tool_ordinal)
            yield RawProviderToolCallStart(
                id=f"raw-tool-start:{tool_call_id}",
                **common,
                tool_call_id=tool_call_id,
                tool_call_name=f"benchmark_tool_{tool_ordinal}",
            )
            yield RawProviderToolCallDelta(
                id=f"raw-tool-delta:{tool_call_id}",
                **common,
                tool_call_id=tool_call_id,
                delta=(
                    '{"call_ordinal":'
                    f"{self._call_ordinal},"
                    '"tool_ordinal":'
                    f"{tool_ordinal}"
                    "}"
                ),
            )
            yield RawProviderToolCallEnd(
                id=f"raw-tool-end:{tool_call_id}",
                **common,
                tool_call_id=tool_call_id,
            )
        yield TransportUsageReport(
            usage_status="reported",
            usage=ModelTokenUsageFact(
                input_tokens=128,
                cached_input_tokens=0,
                output_tokens=256,
                reasoning_output_tokens=0,
                total_tokens=384,
            ),
            reported_model_id="benchmark-model",
        )


class DeterministicWindowSummaryTransport(RawLLMTransport):
    api = "mock"
    binding_id = "pulsara.benchmark.deterministic-context-stream"
    contract_version = "v1"

    def __init__(self, *, summary_characters: int) -> None:
        self._summary_characters = summary_characters

    async def stream(
        self,
        *,
        call: ResolvedModelCall,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[RawProviderStreamItem | TransportUsageReport]:
        del call
        if not context.messages or not context.messages[-1].content:
            raise RuntimeError("window summary benchmark lacks source input")
        source = json.loads(context.messages[-1].content[0])
        entries = source.get("entries")
        if not isinstance(entries, list) or not entries:
            raise RuntimeError("window summary benchmark has no summarized entries")
        source_entry_id = entries[0].get("source_entry_id")
        if not isinstance(source_entry_id, str):
            raise RuntimeError("window summary benchmark source identity is invalid")
        summary = _window_summary_json(
            source_entry_id=source_entry_id,
            target_characters=self._summary_characters,
        )
        common = {
            **event_context.event_fields(),
            "created_at": "2026-01-01T00:00:00.000000Z",
        }
        block_id = f"benchmark-window-summary:{source_entry_id}"
        yield RawProviderTextBlockStart(
            id=f"raw-window-summary-start:{source_entry_id}",
            **common,
            block_id=block_id,
        )
        yield RawProviderTextDelta(
            id=f"raw-window-summary-delta:{source_entry_id}",
            **common,
            block_id=block_id,
            delta=summary,
        )
        yield RawProviderTextBlockEnd(
            id=f"raw-window-summary-end:{source_entry_id}",
            **common,
            block_id=block_id,
        )
        yield TransportUsageReport(
            usage_status="reported",
            usage=ModelTokenUsageFact(
                input_tokens=2_048,
                cached_input_tokens=0,
                output_tokens=max(1, len(summary) // 4),
                reasoning_output_tokens=0,
                total_tokens=2_048 + max(1, len(summary) // 4),
            ),
            reported_model_id="benchmark-model",
        )


class DeterministicSubagentTransport(RawLLMTransport):
    api = "mock"
    binding_id = "pulsara.benchmark.deterministic-context-stream"
    contract_version = "v1"

    def __init__(self, *, specs: tuple[_ChildScriptSpec, ...]) -> None:
        self._specs = specs
        self._call_count_by_run: dict[str, int] = {}

    async def stream(
        self,
        *,
        call: ResolvedModelCall,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[RawProviderStreamItem | TransportUsageReport]:
        del call
        context_text = "\n".join(
            (
                context.system_prompt or "",
                *(
                    content
                    for message in context.messages
                    for content in message.content
                ),
            )
        )
        matches = tuple(
            spec for spec in self._specs if spec.objective_marker in context_text
        )
        if len(matches) != 1:
            raise RuntimeError("subagent benchmark could not identify child objective")
        spec = matches[0]
        call_ordinal = self._call_count_by_run.get(event_context.run_id, 0) + 1
        self._call_count_by_run[event_context.run_id] = call_ordinal
        if call_ordinal > spec.model_calls:
            raise RuntimeError("subagent benchmark child exceeded scripted calls")
        available_tools = {tool.name for tool in context.tools}
        tool_name = (
            "report_agent_result"
            if call_ordinal == spec.model_calls
            else "report_agent_phase"
        )
        if tool_name not in available_tools:
            raise RuntimeError(
                f"subagent benchmark child lacks required tool {tool_name}"
            )
        total_delta_events = spec.target_raw_events - spec.model_calls * 6
        delta_counts = _even_partition(total_delta_events, spec.model_calls)
        delta_count = delta_counts[call_ordinal - 1]
        common = {
            **event_context.event_fields(),
            "created_at": "2026-01-01T00:00:00.000000Z",
        }
        block_id = f"benchmark-child-thinking:{spec.child_id}:{call_ordinal}"
        yield RawProviderThinkingBlockStart(
            id=f"raw-child-thinking-start:{spec.child_id}:{call_ordinal}",
            **common,
            block_id=block_id,
        )
        for delta_ordinal in range(delta_count):
            yield RawProviderThinkingDelta(
                id=(
                    f"raw-child-thinking-delta:{spec.child_id}:"
                    f"{call_ordinal}:{delta_ordinal}"
                ),
                **common,
                block_id=block_id,
                delta=_fixed_width_ascii(
                    call_ordinal,
                    delta_ordinal,
                    24,
                ),
            )
        yield RawProviderThinkingBlockEnd(
            id=f"raw-child-thinking-end:{spec.child_id}:{call_ordinal}",
            **common,
            block_id=block_id,
        )
        tool_call_id = (
            f"tool:benchmark-child:{spec.child_id}:{call_ordinal}"
        )
        arguments = (
            {
                "summary": f"{spec.child_id} explicit benchmark result",
                "output_preview": (
                    f"{spec.child_id} explicit benchmark evidence"
                ),
            }
            if tool_name == "report_agent_result"
            else {
                "phase": f"step-{call_ordinal}",
                "message": (
                    f"{spec.child_id} benchmark progress {call_ordinal}"
                ),
            }
        )
        yield RawProviderToolCallStart(
            id=f"raw-child-tool-start:{spec.child_id}:{call_ordinal}",
            **common,
            tool_call_id=tool_call_id,
            tool_call_name=tool_name,
        )
        yield RawProviderToolCallDelta(
            id=f"raw-child-tool-delta:{spec.child_id}:{call_ordinal}",
            **common,
            tool_call_id=tool_call_id,
            delta=json.dumps(arguments, sort_keys=True, separators=(",", ":")),
        )
        yield RawProviderToolCallEnd(
            id=f"raw-child-tool-end:{spec.child_id}:{call_ordinal}",
            **common,
            tool_call_id=tool_call_id,
        )
        yield TransportUsageReport(
            usage_status="reported",
            usage=ModelTokenUsageFact(
                input_tokens=128,
                cached_input_tokens=0,
                output_tokens=max(1, delta_count * 6),
                reasoning_output_tokens=0,
                total_tokens=128 + max(1, delta_count * 6),
            ),
            reported_model_id="benchmark-model",
        )


async def run_context_preparation_sample(
    *,
    scenario: (
        SupportedContextScenario
        | CheckpointRebaseRestartScenario
        | SingleLongCompactionScenario
        | SubagentTwoChildrenScenario
    ),
    execution_case: CheckpointRestartCase | None,
    mode: str,
    dsn: str,
    workspace_root: Path,
    sample_identity: str,
) -> ContextPreparationObservation:
    """Run one trajectory and measure real context preparation at each point."""

    if isinstance(scenario, CheckpointRebaseRestartScenario):
        if execution_case is None:
            raise ValueError("checkpoint benchmark requires one execution case")
        return await _run_checkpoint_rebase_restart_sample(
            scenario=scenario,
            execution_case=execution_case,
            dsn=dsn,
            workspace_root=workspace_root,
            sample_identity=sample_identity,
        )
    if isinstance(scenario, SingleLongCompactionScenario):
        if execution_case is not None:
            raise ValueError("single compaction scenario has no execution matrix")
        return await _run_single_long_compaction_sample(
            scenario=scenario,
            mode=mode,
            dsn=dsn,
            workspace_root=workspace_root,
            sample_identity=sample_identity,
        )
    if isinstance(scenario, SubagentTwoChildrenScenario):
        if execution_case is not None:
            raise ValueError("subagent scenario has no execution matrix")
        return await _run_subagent_two_children_sample(
            scenario=scenario,
            mode=mode,
            dsn=dsn,
            workspace_root=workspace_root,
            sample_identity=sample_identity,
        )
    if execution_case is not None:
        raise ValueError("common context scenarios do not accept execution cases")

    runtime_session_id = f"runtime:benchmark:{_hex_identity(sample_identity)}"
    event_context = EventContext(
        run_id=f"run:benchmark:{_hex_identity(sample_identity + ':run')}",
        turn_id=f"turn:benchmark:{_hex_identity(sample_identity + ':turn')}",
        reply_id=f"reply:benchmark:{_hex_identity(sample_identity + ':reply')}",
    )
    runtime_session = _runtime_session(
        dsn=dsn,
        workspace_root=workspace_root,
        runtime_session_id=runtime_session_id,
    )
    llm_config = _benchmark_llm_config()
    target_runtime = LLMRuntime(
        config=llm_config,
        registry=_empty_transport_registry(),
    )
    target = target_runtime.resolve_target(role=ModelRole.PRO)
    run = await bootstrap_benchmark_context_run(
        runtime_session,
        event_context=event_context,
        target=target,
    )
    control_owner = RunModelCallControlOwner(
        run_id=event_context.run_id,
        activation=run.activation,
        segment_id=run.working_set.process_segment_id or "benchmark-segment",
        segment_generation=run.activation.segment_generation,
    )
    run.working_set.model_call_control_owner = control_owner
    steps, repeat_final_compile = _trajectory_steps(scenario)
    observations: list[ContextCompilePointObservation] = []
    generated_semantic_delta_count = 0
    generated_tool_result_count = 0
    try:
        # Prime acceleration-only graph/checkpoint state outside the measured
        # trajectory. Otherwise the first compile would include a one-time
        # checkpoint commit while later points would not be comparable.
        await _measure_compile_point(
            runtime_session=runtime_session,
            run=run,
            call_ordinal=0,
            point_id="fixture_acceleration_priming",
        )
        for call_ordinal, step in enumerate(steps, start=1):
            await _append_model_step(
                runtime_session=runtime_session,
                run=run,
                llm_config=llm_config,
                call_ordinal=call_ordinal,
                step=step,
            )
            generated_semantic_delta_count += step.semantic_delta_events
            generated_tool_result_count += len(step.tool_result_characters)
            if mode == "process_cold":
                runtime_session.close()
                runtime_session = _runtime_session(
                    dsn=dsn,
                    workspace_root=workspace_root,
                    runtime_session_id=runtime_session_id,
                )
                run = rebind_benchmark_context_run(
                    runtime_session,
                    event_context=event_context,
                    target=target,
                )
                run.working_set.model_call_control_owner = (
                    RunModelCallControlOwner(
                        run_id=event_context.run_id,
                        activation=run.activation,
                        segment_id=(
                            run.working_set.process_segment_id
                            or "benchmark-segment"
                        ),
                        segment_generation=run.activation.segment_generation,
                    )
                )
            elif mode == "verified_artifact_cache_warm":
                await _stabilize_acceleration(runtime_session)
                await _measure_compile_point(
                    runtime_session=runtime_session,
                    run=run,
                    call_ordinal=call_ordinal,
                    point_id=f"cache_warmup_after_model_call_{call_ordinal}",
                )
            await _stabilize_acceleration(runtime_session)
            observations.append(
                await _measure_compile_point(
                    runtime_session=runtime_session,
                    run=run,
                    call_ordinal=call_ordinal,
                    point_id=f"after_model_call_{call_ordinal}",
                )
            )
        for repeat_ordinal in range(1, repeat_final_compile + 1):
            observations.append(
                await _measure_compile_point(
                    runtime_session=runtime_session,
                    run=run,
                    call_ordinal=len(steps),
                    point_id=f"repeat_final_compile_{repeat_ordinal}",
                )
            )
        final_point = observations[-1]
        await _stabilize_acceleration(runtime_session)
        verification = await _measure_compile_point(
            runtime_session=runtime_session,
            run=run,
            call_ordinal=len(steps),
            point_id=final_point.point_id,
        )
        repeated_equal = (
            verification.normalized_transcript_fingerprint
            == final_point.normalized_transcript_fingerprint
            and verification.provider_payload_fingerprint
            == final_point.provider_payload_fingerprint
            and verification.authority_plan_fingerprint
            == final_point.authority_plan_fingerprint
        )
        return ContextPreparationObservation(
            scenario_id=scenario.scenario_id,
            mode=mode,
            compile_points=tuple(observations),
            generated_semantic_delta_count=generated_semantic_delta_count,
            generated_tool_result_count=generated_tool_result_count,
            repeated_final_compile_semantics_equal=repeated_equal,
        )
    finally:
        drain_deadline = monotonic() + 30.0
        await runtime_session.context_input_io_service.drain_pending(
            deadline_monotonic=drain_deadline
        )
        await runtime_session.subagent_graph_checkpoint_service.drain_pending(
            deadline_monotonic=drain_deadline
        )
        runtime_session.close()


async def _run_checkpoint_rebase_restart_sample(
    *,
    scenario: CheckpointRebaseRestartScenario,
    execution_case: CheckpointRestartCase,
    dsn: str,
    workspace_root: Path,
    sample_identity: str,
) -> ContextPreparationObservation:
    """Exercise transcript checkpoint adoption, cold restore, and rebase."""

    runtime_session_id = f"runtime:benchmark:{_hex_identity(sample_identity)}"
    event_context = EventContext(
        run_id=f"run:benchmark:{_hex_identity(sample_identity + ':run')}",
        turn_id=f"turn:benchmark:{_hex_identity(sample_identity + ':turn')}",
        reply_id=f"reply:benchmark:{_hex_identity(sample_identity + ':reply')}",
    )
    runtime_session = _runtime_session(
        dsn=dsn,
        workspace_root=workspace_root,
        runtime_session_id=runtime_session_id,
    )
    llm_config = _benchmark_llm_config()
    target = LLMRuntime(
        config=llm_config,
        registry=_empty_transport_registry(),
    ).resolve_target(role=ModelRole.PRO)
    run = await bootstrap_benchmark_context_run(
        runtime_session,
        event_context=event_context,
        target=target,
    )
    run.working_set.model_call_control_owner = _control_owner(run)
    checkpoint_by_logical_id: dict[str, CommittedTranscriptCheckpoint] = {}
    generated_semantic_delta_count = 0
    observations: list[ContextCompilePointObservation] = []
    deleted_checkpoint_id: str | None = None
    expected_selected_checkpoint_id: str | None = None
    pre_reopen_provider_fingerprint: str | None = None
    try:
        checkpoints_by_high_water = {
            checkpoint.logical_semantic_high_water: checkpoint
            for checkpoint in scenario.ledger.checkpoints
        }
        previous_logical_high_water = 0
        call_ordinal = 0
        for logical_high_water in (
            *checkpoints_by_high_water,
            scenario.ledger.semantic_events_before_close,
        ):
            call_ordinal += 1
            semantic_delta_count = logical_high_water - previous_logical_high_water
            previous_logical_high_water = logical_high_water
            await _append_model_step(
                runtime_session=runtime_session,
                run=run,
                llm_config=llm_config,
                call_ordinal=call_ordinal,
                step=_TrajectoryStep(
                    semantic_delta_events=semantic_delta_count,
                    characters_per_delta=(
                        scenario.ledger.characters_per_semantic_delta
                    ),
                    tool_result_characters=(),
                ),
            )
            generated_semantic_delta_count += semantic_delta_count
            checkpoint_fixture = checkpoints_by_high_water.get(
                logical_high_water
            )
            if checkpoint_fixture is None:
                continue
            committed = await _force_transcript_checkpoint(
                runtime_session=runtime_session,
                run=run,
            )
            checkpoint_by_logical_id[checkpoint_fixture.checkpoint_id] = committed

        await _stabilize_acceleration(runtime_session)
        pre_reopen = await _measure_compile_point(
            runtime_session=runtime_session,
            run=run,
            call_ordinal=call_ordinal,
            point_id="before_reopen_reference",
        )
        pre_reopen_provider_fingerprint = pre_reopen.provider_payload_fingerprint

        preferred_fixture = next(
            checkpoint
            for checkpoint in scenario.ledger.checkpoints
            if checkpoint.preferred
        )
        preferred_committed = checkpoint_by_logical_id[
            preferred_fixture.checkpoint_id
        ]
        if isinstance(execution_case, MissingCheckpointRebaseCase):
            _delete_checkpoint_artifacts(
                runtime_session=runtime_session,
                committed=preferred_committed,
            )
            deleted_checkpoint_id = (
                preferred_committed.installed.prepared.candidate.checkpoint_id
            )
            expected_selected_checkpoint_id = checkpoint_by_logical_id[
                execution_case.expected_rebase_checkpoint_id
            ].installed.prepared.candidate.checkpoint_id
        elif isinstance(
            execution_case,
            (PreferredCheckpointCase, ColdNoCacheCase),
        ):
            expected_selected_checkpoint_id = (
                preferred_committed.installed.prepared.candidate.checkpoint_id
            )
        else:
            raise TypeError(
                f"unsupported checkpoint execution case: {execution_case.case_id}"
            )

        runtime_session.close()
        runtime_session = _runtime_session(
            dsn=dsn,
            workspace_root=workspace_root,
            runtime_session_id=runtime_session_id,
        )
        run = rebind_benchmark_context_run(
            runtime_session,
            event_context=event_context,
            target=target,
        )
        run.working_set.model_call_control_owner = _control_owner(run)
        await _stabilize_acceleration(runtime_session)
        observations.append(
            await _measure_compile_point(
                runtime_session=runtime_session,
                run=run,
                call_ordinal=call_ordinal,
                point_id="immediately_after_reopen",
            )
        )
        for post_ordinal in range(
            1,
            scenario.ledger.restart.post_reopen_model_calls + 1,
        ):
            call_ordinal += 1
            delta_count = (
                scenario.ledger.restart.post_reopen_semantic_delta_events_per_call
            )
            await _append_model_step(
                runtime_session=runtime_session,
                run=run,
                llm_config=llm_config,
                call_ordinal=call_ordinal,
                step=_TrajectoryStep(
                    semantic_delta_events=delta_count,
                    characters_per_delta=(
                        scenario.ledger.characters_per_semantic_delta
                    ),
                    tool_result_characters=(),
                ),
            )
            generated_semantic_delta_count += delta_count
            await _stabilize_acceleration(runtime_session)
            observations.append(
                await _measure_compile_point(
                    runtime_session=runtime_session,
                    run=run,
                    call_ordinal=call_ordinal,
                    point_id=f"after_post_reopen_model_call_{post_ordinal}",
                )
            )

        final = observations[-1]
        verification = await _measure_compile_point(
            runtime_session=runtime_session,
            run=run,
            call_ordinal=call_ordinal,
            point_id=final.point_id,
        )
        repeated_equal = (
            verification.normalized_transcript_fingerprint
            == final.normalized_transcript_fingerprint
            and verification.provider_payload_fingerprint
            == final.provider_payload_fingerprint
            and verification.authority_plan_fingerprint
            == final.authority_plan_fingerprint
        )
        return ContextPreparationObservation(
            scenario_id=scenario.scenario_id,
            mode="default",
            compile_points=tuple(observations),
            generated_semantic_delta_count=generated_semantic_delta_count,
            generated_tool_result_count=0,
            repeated_final_compile_semantics_equal=repeated_equal,
            generated_checkpoint_ids=tuple(
                checkpoint_by_logical_id[item.checkpoint_id]
                .installed.prepared.candidate.checkpoint_id
                for item in scenario.ledger.checkpoints
            ),
            deleted_checkpoint_id=deleted_checkpoint_id,
            expected_selected_checkpoint_id=expected_selected_checkpoint_id,
            reopen_provider_semantics_equal=(
                pre_reopen_provider_fingerprint
                == observations[0].provider_payload_fingerprint
            ),
        )
    finally:
        drain_deadline = monotonic() + 30.0
        await runtime_session.context_input_io_service.drain_pending(
            deadline_monotonic=drain_deadline
        )
        await runtime_session.transcript_projection_checkpoint_service.drain_pending(
            deadline_monotonic=drain_deadline
        )
        await runtime_session.subagent_graph_checkpoint_service.drain_pending(
            deadline_monotonic=drain_deadline
        )
        runtime_session.close()


async def _run_single_long_compaction_sample(
    *,
    scenario: SingleLongCompactionScenario,
    mode: str,
    dsn: str,
    workspace_root: Path,
    sample_identity: str,
) -> ContextPreparationObservation:
    """Measure one real same-run LLM window compaction and its new base."""

    runtime_session_id = f"runtime:benchmark:{_hex_identity(sample_identity)}"
    event_context = EventContext(
        run_id=f"run:benchmark:{_hex_identity(sample_identity + ':run')}",
        turn_id=f"turn:benchmark:{_hex_identity(sample_identity + ':turn')}",
        reply_id=f"reply:benchmark:{_hex_identity(sample_identity + ':reply')}",
    )
    runtime_session = _runtime_session(
        dsn=dsn,
        workspace_root=workspace_root,
        runtime_session_id=runtime_session_id,
    )
    llm_config = _benchmark_llm_config(default_output_tokens=4_096)
    target = LLMRuntime(
        config=llm_config,
        registry=_empty_transport_registry(),
    ).resolve_target(role=ModelRole.PRO)
    run = await bootstrap_benchmark_context_run(
        runtime_session,
        event_context=event_context,
        target=target,
    )
    run.working_set.model_call_control_owner = _control_owner(run)
    observations: list[ContextCompilePointObservation] = []
    generated_semantic_delta_count = 0
    generated_tool_result_count = 0
    compaction_service: ContextWindowCompactionService | None = None
    compaction_source_artifact_verified = False
    call_ordinal = 0
    try:
        await _measure_compile_point(
            runtime_session=runtime_session,
            run=run,
            call_ordinal=0,
            point_id="fixture_acceleration_priming",
        )
        delta_counts = _even_partition(
            scenario.ledger.semantic_delta_events_before_compaction,
            scenario.ledger.model_calls_before_compaction,
        )
        for delta_count in delta_counts:
            call_ordinal += 1
            await _append_model_step(
                runtime_session=runtime_session,
                run=run,
                llm_config=llm_config,
                call_ordinal=call_ordinal,
                step=_TrajectoryStep(
                    semantic_delta_events=delta_count,
                    characters_per_delta=(
                        scenario.ledger.characters_per_semantic_delta
                    ),
                    tool_result_characters=(
                        (scenario.ledger.prior_tool_observation_characters,)
                    ),
                ),
            )
        generated_semantic_delta_count += (
            scenario.ledger.semantic_delta_events_before_compaction
        )
        generated_tool_result_count += scenario.ledger.prior_tool_observations
        if mode == "process_cold":
            runtime_session, run = _reopen_context_run(
                runtime_session=runtime_session,
                dsn=dsn,
                workspace_root=workspace_root,
                runtime_session_id=runtime_session_id,
                event_context=event_context,
                target=target,
            )
        await _stabilize_acceleration(runtime_session)
        before = await _prepare_compile_point(
            runtime_session=runtime_session,
            run=run,
            call_ordinal=call_ordinal,
            point_id="before_compaction",
        )
        observations.append(before.observation)

        summary_registry = LLMTransportRegistry(production_mode=True)
        summary_registry.register(
            SanitizingLLMTransport(
                DeterministicWindowSummaryTransport(
                    summary_characters=(
                        scenario.ledger.window_compaction.summary_characters
                    )
                )
            )
        )
        compaction_service = ContextWindowCompactionService(
            runtime_session=runtime_session,
            llm_runtime=LLMRuntime(
                config=llm_config,
                registry=summary_registry,
            ),
        )
        runtime_session.window_compaction_service = compaction_service
        outcome = None
        compaction_input = before
        for retry_ordinal in range(4):
            planning = prepare_current_run_projection_planning_input(
                run_id=event_context.run_id,
                run_start_sequence=run.working_set.run_start_sequence,
                window=compaction_input.prepared.active_window,
                current_projection=compaction_input.prepared.projection_state,
                canonical_slice=compaction_input.prepared.authority_slice,
                transcript=(
                    compaction_input.prepared.normalized_transcript.transcript
                ),
                tool_result_units=(
                    compaction_input.prepared.normalized_transcript.tool_result_units
                ),
                context_budget=target.context_budget,
                allocation_policy=(
                    run.working_set.long_horizon_contract.window_policy
                ),
                estimator=target.fact.token_estimator,
                pending_interaction=False,
                tool_call_in_flight=False,
            )
            outcome = await compaction_service.compact(
                WindowCompactionRequest(
                    event_context=event_context,
                    state=run.state,
                    run_contract=run.working_set.long_horizon_contract,
                    source_window=compaction_input.prepared.active_window,
                    source_projection=compaction_input.prepared.projection_state,
                    transcript=(
                        compaction_input.prepared.normalized_transcript.transcript
                    ),
                    tool_result_units=(
                        compaction_input.prepared.normalized_transcript.tool_result_units
                    ),
                    rendered_tool_results=compaction_input.rendered,
                    prepared_rollups=(),
                    protection_facts=planning.protection_facts,
                    source_through_sequence=(
                        compaction_input.prepared.authority_slice.through_sequence
                    ),
                    source_context_fingerprint=(
                        provider_neutral_payload_fingerprint(
                            compaction_input.compiled.llm_context
                        )
                    ),
                    estimated_tokens_before=(
                        compaction_input.compiled.final_token_estimate.total_input_tokens
                    ),
                    non_transcript_baseline_tokens=(
                        compaction_input.compiled.budget.non_transcript_baseline_tokens
                    ),
                    transcript_tokens_before=(
                        compaction_input.compiled.budget.transcript_estimated_tokens
                    ),
                    force=True,
                )
            )
            if outcome.status == "compacted":
                break
            if outcome.status not in {"phase_transitioned", "source_stale"}:
                raise RuntimeError(
                    "benchmark window compaction did not complete: "
                    f"{outcome.status}:{outcome.reason_code}"
                )
            await _stabilize_acceleration(runtime_session)
            compaction_input = await _prepare_compile_point(
                runtime_session=runtime_session,
                run=run,
                call_ordinal=call_ordinal,
                point_id=f"compaction_retry_{retry_ordinal + 1}",
            )
        if outcome is None or outcome.status != "compacted":
            raise RuntimeError("benchmark window compaction retries did not converge")
        terminal = runtime_session.event_log.get_by_id(outcome.terminal_event_id or "")
        if not isinstance(terminal, ContextWindowCompactionCompletedEvent):
            raise RuntimeError("benchmark compaction terminal event is unavailable")
        started = runtime_session.event_log.get_by_id(terminal.started_event_id)
        if not isinstance(started, ContextWindowCompactionStartedEvent):
            raise RuntimeError("benchmark compaction Started event is unavailable")
        source_text = runtime_session.archive.get_text(
            started.plan.source_document_artifact_id,
            session_id=runtime_session.runtime_session_id,
        )
        source_payload = json.loads(source_text)
        compaction_source_artifact_verified = (
            source_payload.get("document_fingerprint")
            == started.plan.source_document_fingerprint
        )
        await _ingest_tool_result_projections(
            runtime_session=runtime_session,
            run=run,
            resolved_call=before.resolved_call,
            event_context=event_context,
        )

        if mode == "process_cold":
            runtime_session, run = _reopen_context_run(
                runtime_session=runtime_session,
                dsn=dsn,
                workspace_root=workspace_root,
                runtime_session_id=runtime_session_id,
                event_context=event_context,
                target=target,
            )
            compaction_service = None
        await _stabilize_acceleration(runtime_session)
        observations.append(
            await _measure_compile_point(
                runtime_session=runtime_session,
                run=run,
                call_ordinal=call_ordinal,
                point_id="immediately_after_compaction",
            )
        )

        for post_ordinal in range(
            1,
            scenario.ledger.post_compaction_model_calls + 1,
        ):
            call_ordinal += 1
            delta_count = (
                scenario.ledger.post_compaction_semantic_delta_events_per_call
            )
            await _append_model_step(
                runtime_session=runtime_session,
                run=run,
                llm_config=llm_config,
                call_ordinal=call_ordinal,
                step=_TrajectoryStep(
                    semantic_delta_events=delta_count,
                    characters_per_delta=(
                        scenario.ledger.characters_per_semantic_delta
                    ),
                    tool_result_characters=(),
                ),
            )
            generated_semantic_delta_count += delta_count
            if mode == "process_cold":
                runtime_session, run = _reopen_context_run(
                    runtime_session=runtime_session,
                    dsn=dsn,
                    workspace_root=workspace_root,
                    runtime_session_id=runtime_session_id,
                    event_context=event_context,
                    target=target,
                )
            await _stabilize_acceleration(runtime_session)
            observations.append(
                await _measure_compile_point(
                    runtime_session=runtime_session,
                    run=run,
                    call_ordinal=call_ordinal,
                    point_id=(
                        "after_first_post_compaction_call"
                        if post_ordinal == 1
                        else "after_second_post_compaction_call"
                    ),
                )
            )

        final = observations[-1]
        verification = await _measure_compile_point(
            runtime_session=runtime_session,
            run=run,
            call_ordinal=call_ordinal,
            point_id=final.point_id,
        )
        repeated_equal = (
            verification.normalized_transcript_fingerprint
            == final.normalized_transcript_fingerprint
            and verification.provider_payload_fingerprint
            == final.provider_payload_fingerprint
            and verification.authority_plan_fingerprint
            == final.authority_plan_fingerprint
        )
        return ContextPreparationObservation(
            scenario_id=scenario.scenario_id,
            mode=mode,
            compile_points=tuple(observations),
            generated_semantic_delta_count=generated_semantic_delta_count,
            generated_tool_result_count=generated_tool_result_count,
            repeated_final_compile_semantics_equal=repeated_equal,
            compaction_status=outcome.status,
            compaction_source_artifact_verified=(
                compaction_source_artifact_verified
            ),
        )
    finally:
        drain_deadline = monotonic() + 30.0
        if compaction_service is not None:
            await compaction_service.drain_pending(
                deadline_monotonic=drain_deadline
            )
        await runtime_session.context_input_io_service.drain_pending(
            deadline_monotonic=drain_deadline
        )
        await runtime_session.subagent_graph_checkpoint_service.drain_pending(
            deadline_monotonic=drain_deadline
        )
        runtime_session.close()


async def _run_subagent_two_children_sample(
    *,
    scenario: SubagentTwoChildrenScenario,
    mode: str,
    dsn: str,
    workspace_root: Path,
    sample_identity: str,
) -> ContextPreparationObservation:
    """Run two real scripted child agents and compile their parent graph facts."""

    runtime_session_id = f"runtime:benchmark:{_hex_identity(sample_identity)}"
    event_context = EventContext(
        run_id=f"run:benchmark:{_hex_identity(sample_identity + ':run')}",
        turn_id=f"turn:benchmark:{_hex_identity(sample_identity + ':turn')}",
        reply_id=f"reply:benchmark:{_hex_identity(sample_identity + ':reply')}",
    )
    runtime_session = _runtime_session(
        dsn=dsn,
        workspace_root=workspace_root,
        runtime_session_id=runtime_session_id,
    )
    llm_config = _benchmark_llm_config(default_output_tokens=4_096)
    target = LLMRuntime(
        config=llm_config,
        registry=_empty_transport_registry(),
    ).resolve_target(role=ModelRole.PRO)
    run = await bootstrap_benchmark_context_run(
        runtime_session,
        event_context=event_context,
        target=target,
    )
    run.working_set.model_call_control_owner = _control_owner(run)
    specs = tuple(
        _ChildScriptSpec(
            child_id=child.child_id,
            objective_marker=f"BENCHMARK_CHILD:{child.child_id}",
            model_calls=child.model_calls,
            target_raw_events=child.target_raw_events,
        )
        for child in scenario.ledger.children
    )
    child_transport = DeterministicSubagentTransport(specs=specs)
    agent = _bind_subagent_agent(
        runtime_session=runtime_session,
        run=run,
        llm_config=llm_config,
        child_transport=child_transport,
    )
    subagent_runtime = agent.subagent_runtime
    if subagent_runtime is None:
        raise RuntimeError("subagent benchmark failed to bind its graph runtime")
    task_id_by_child: dict[str, str] = {}
    for child in scenario.ledger.children:
        task_id = f"subagent_task:benchmark:{child.child_id}"
        dependencies = tuple(
            task_id_by_child[dependency] for dependency in child.depends_on
        )
        await subagent_runtime.create_task(
            objective=f"BENCHMARK_CHILD:{child.child_id}",
            event_context=event_context,
            task_id=task_id,
            profile_id=(
                "review_worker"
                if child.child_id == "review"
                else "verification_worker"
            ),
            task_key=child.child_id,
            depends_on=dependencies,
        )
        task_id_by_child[child.child_id] = task_id

    observations: list[ContextCompilePointObservation] = []
    generated_semantic_delta_count = 0
    generated_tool_result_count = 0
    child_runtime_session_ids: list[str] = []
    child_terminal_event_ids: list[str] = []
    child_result_ids: list[str] = []
    completed_parent_events: list[SubagentRunCompletedEvent] = []
    started_parent_events: dict[str, SubagentRunStartedEvent] = {}
    checkpoint_id: str | None = None
    child_by_trigger = {
        4: scenario.ledger.children[0],
        8: scenario.ledger.children[1],
    }
    try:
        await _measure_compile_point(
            runtime_session=runtime_session,
            run=run,
            call_ordinal=0,
            point_id="fixture_acceleration_priming",
        )
        for call_ordinal in range(
            1,
            scenario.ledger.parent.model_calls + 1,
        ):
            tool_result_characters = (
                (2_048,)
                if call_ordinal <= scenario.ledger.parent.tool_calls
                else ()
            )
            await _append_model_step(
                runtime_session=runtime_session,
                run=run,
                llm_config=llm_config,
                call_ordinal=call_ordinal,
                step=_TrajectoryStep(
                    semantic_delta_events=(
                        scenario.ledger.parent.semantic_delta_events_per_call
                    ),
                    characters_per_delta=(
                        scenario.ledger.parent.characters_per_semantic_delta
                    ),
                    tool_result_characters=tool_result_characters,
                ),
            )
            generated_semantic_delta_count += (
                scenario.ledger.parent.semantic_delta_events_per_call
            )
            generated_tool_result_count += len(tool_result_characters)
            if mode == "process_cold":
                runtime_session, run, agent = _reopen_subagent_context_run(
                    runtime_session=runtime_session,
                    dsn=dsn,
                    workspace_root=workspace_root,
                    runtime_session_id=runtime_session_id,
                    event_context=event_context,
                    target=target,
                    llm_config=llm_config,
                    child_transport=child_transport,
                )
                subagent_runtime = agent.subagent_runtime
                if subagent_runtime is None:
                    raise RuntimeError("cold reopen lost subagent graph runtime")
            await _stabilize_acceleration(runtime_session)
            observations.append(
                await _measure_compile_point(
                    runtime_session=runtime_session,
                    run=run,
                    call_ordinal=call_ordinal,
                    point_id=f"after_model_call_{call_ordinal}",
                )
            )

            child = child_by_trigger.get(call_ordinal)
            if child is None:
                continue
            task = next(
                item
                for item in subagent_runtime.tasks
                if item.task_id == task_id_by_child[child.child_id]
            )
            child_run = await subagent_runtime.start_task(
                task.task_id,
                event_context=event_context,
                spawn_initiator_kind="scheduler",
                spawn_initiator_id=task.task_id,
            )
            result = await _await_subagent_completion(
                subagent_runtime,
                child_run.subagent_run_id,
            )
            generated_semantic_delta_count += (
                child.target_raw_events - child.model_calls * 6
            )
            generated_tool_result_count += child.tool_calls
            child_runtime_session_ids.append(
                child_run.child_runtime_session_id
            )
            child_result_ids.append(result.result_id)
            completed = runtime_session.event_log.get_by_id(
                next(
                    item.provenance.terminal_event_id
                    for item in subagent_runtime.runs
                    if item.subagent_run_id == child_run.subagent_run_id
                )
                or ""
            )
            if not isinstance(completed, SubagentRunCompletedEvent):
                raise RuntimeError("subagent benchmark lacks parent completion fact")
            completed_parent_events.append(completed)
            terminal_ref = completed.result_handoff.child_terminal_reference
            child_terminal_event_ids.append(terminal_ref.terminal_event_id)
            started = runtime_session.event_log.get_by_id(
                child_run.provenance.created_event_id
            )
            if not isinstance(started, SubagentRunStartedEvent):
                raise RuntimeError("subagent benchmark lacks parent start fact")
            started_parent_events[child.child_id] = started

            if child.child_id == scenario.ledger.children[-1].child_id:
                checkpoint = (
                    await runtime_session.subagent_graph_checkpoint_service.checkpoint_for_admission(
                        requested_through_sequence=(
                            runtime_session.event_log.next_sequence() - 1
                        )
                    )
                )
                checkpoint_id = checkpoint.selected_checkpoint_id
            if mode == "process_cold":
                runtime_session, run, agent = _reopen_subagent_context_run(
                    runtime_session=runtime_session,
                    dsn=dsn,
                    workspace_root=workspace_root,
                    runtime_session_id=runtime_session_id,
                    event_context=event_context,
                    target=target,
                    llm_config=llm_config,
                    child_transport=child_transport,
                )
                subagent_runtime = agent.subagent_runtime
                if subagent_runtime is None:
                    raise RuntimeError("cold child reopen lost subagent graph runtime")
            await _stabilize_acceleration(runtime_session)
            observations.append(
                await _measure_compile_point(
                    runtime_session=runtime_session,
                    run=run,
                    call_ordinal=call_ordinal,
                    point_id=f"after_child_{child.child_id}_result",
                )
            )

        final = observations[-1]
        verification = await _measure_compile_point(
            runtime_session=runtime_session,
            run=run,
            call_ordinal=scenario.ledger.parent.model_calls,
            point_id=final.point_id,
        )
        repeated_equal = (
            verification.normalized_transcript_fingerprint
            == final.normalized_transcript_fingerprint
            and verification.provider_payload_fingerprint
            == final.provider_payload_fingerprint
            and verification.authority_plan_fingerprint
            == final.authority_plan_fingerprint
            and verification.subagent_graph_semantic_fingerprint
            == final.subagent_graph_semantic_fingerprint
        )
        child_terminal_references_exact = all(
            isinstance(
                subagent_runtime.child_event_log(event.subagent_run_id).get_by_id(
                    event.result_handoff.child_terminal_reference.terminal_event_id
                ),
                RunEndEvent,
            )
            for event in completed_parent_events
        )
        child_ledger_identity_isolated = (
            len(set(child_runtime_session_ids)) == len(child_runtime_session_ids)
            and runtime_session_id not in child_runtime_session_ids
            and all(
                all(
                    event.run_id != event_context.run_id
                    for event in subagent_runtime.child_event_log(
                        completed.subagent_run_id
                    ).iter()
                )
                for completed in completed_parent_events
            )
        )
        review_completed = completed_parent_events[0]
        verify_started = started_parent_events[
            scenario.ledger.children[1].child_id
        ]
        child_dependency_order_valid = (
            review_completed.sequence is not None
            and verify_started.sequence is not None
            and review_completed.sequence < verify_started.sequence
        )
        return ContextPreparationObservation(
            scenario_id=scenario.scenario_id,
            mode=mode,
            compile_points=tuple(observations),
            generated_semantic_delta_count=generated_semantic_delta_count,
            generated_tool_result_count=generated_tool_result_count,
            repeated_final_compile_semantics_equal=repeated_equal,
            child_runtime_session_ids=tuple(child_runtime_session_ids),
            child_terminal_event_ids=tuple(child_terminal_event_ids),
            child_result_ids=tuple(child_result_ids),
            subagent_graph_checkpoint_id=checkpoint_id,
            child_ledger_identity_isolated=child_ledger_identity_isolated,
            child_terminal_references_exact=child_terminal_references_exact,
            child_dependency_order_valid=child_dependency_order_valid,
        )
    finally:
        drain_deadline = monotonic() + 30.0
        await runtime_session.context_input_io_service.drain_pending(
            deadline_monotonic=drain_deadline
        )
        await runtime_session.subagent_graph_checkpoint_service.drain_pending(
            deadline_monotonic=drain_deadline
        )
        runtime_session.close()


def _bind_subagent_agent(
    *,
    runtime_session: RuntimeSession,
    run: BenchmarkContextRun,
    llm_config: LLMConfig,
    child_transport: DeterministicSubagentTransport,
) -> AgentRuntime:
    registry = LLMTransportRegistry(production_mode=True)
    registry.register(SanitizingLLMTransport(child_transport))
    agent = AgentRuntime(
        runtime_session=runtime_session,
        llm_runtime=LLMRuntime(config=llm_config, registry=registry),
        capability_runtime=CapabilityRuntime(),
    )
    if agent.subagent_runtime is None:
        raise RuntimeError("subagent benchmark AgentRuntime lacks graph runtime")
    exposure = run.working_set.effective_exposure_plan
    if exposure is None:
        raise RuntimeError("subagent benchmark lacks frozen parent exposure")
    permission_mode = PermissionMode.BYPASS_PERMISSIONS
    agent.subagent_runtime.refresh_parent_capability_snapshot(
        exposure=exposure,
        permission_mode=permission_mode.value,
        permission_policy=preset_to_policy(permission_mode).to_dict(),
    )
    return agent


def _reopen_subagent_context_run(
    *,
    runtime_session: RuntimeSession,
    dsn: str,
    workspace_root: Path,
    runtime_session_id: str,
    event_context: EventContext,
    target: ResolvedModelTarget,
    llm_config: LLMConfig,
    child_transport: DeterministicSubagentTransport,
) -> tuple[RuntimeSession, BenchmarkContextRun, AgentRuntime]:
    reopened, run = _reopen_context_run(
        runtime_session=runtime_session,
        dsn=dsn,
        workspace_root=workspace_root,
        runtime_session_id=runtime_session_id,
        event_context=event_context,
        target=target,
    )
    return (
        reopened,
        run,
        _bind_subagent_agent(
            runtime_session=reopened,
            run=run,
            llm_config=llm_config,
            child_transport=child_transport,
        ),
    )


async def _await_subagent_completion(
    subagent_runtime,
    subagent_run_id: str,
):
    deadline = monotonic() + 120.0
    while monotonic() < deadline:
        result = subagent_runtime.result_for_run(subagent_run_id)
        if result is not None:
            return result
        run = next(
            item
            for item in subagent_runtime.runs
            if item.subagent_run_id == subagent_run_id
        )
        if run.status in {"failed", "cancelled"}:
            raise RuntimeError(
                f"subagent benchmark child terminated as {run.status}"
            )
        await asyncio.sleep(0.01)
    raise TimeoutError("subagent benchmark child completion timed out")


def _control_owner(run: BenchmarkContextRun) -> RunModelCallControlOwner:
    return RunModelCallControlOwner(
        run_id=run.event_context.run_id,
        activation=run.activation,
        segment_id=run.working_set.process_segment_id or "benchmark-segment",
        segment_generation=run.activation.segment_generation,
    )


def _reopen_context_run(
    *,
    runtime_session: RuntimeSession,
    dsn: str,
    workspace_root: Path,
    runtime_session_id: str,
    event_context: EventContext,
    target: ResolvedModelTarget,
) -> tuple[RuntimeSession, BenchmarkContextRun]:
    runtime_session.close()
    reopened = _runtime_session(
        dsn=dsn,
        workspace_root=workspace_root,
        runtime_session_id=runtime_session_id,
    )
    run = rebind_benchmark_context_run(
        reopened,
        event_context=event_context,
        target=target,
    )
    run.working_set.model_call_control_owner = _control_owner(run)
    return reopened, run


async def _force_transcript_checkpoint(
    *,
    runtime_session: RuntimeSession,
    run: BenchmarkContextRun,
) -> CommittedTranscriptCheckpoint:
    committed = (
        await runtime_session.transcript_projection_checkpoint_service.checkpoint_if_needed(
            context=run.event_context,
            run_seed_semantic=run.working_set.run_transcript_seed_semantic,
            run_seed_reference=run.working_set.run_transcript_seed_reference,
            force_for_admission=True,
        )
    )
    if not isinstance(committed, CommittedTranscriptCheckpoint):
        raise RuntimeError("benchmark forced checkpoint did not commit")
    return committed


def _delete_checkpoint_artifacts(
    *,
    runtime_session: RuntimeSession,
    committed: CommittedTranscriptCheckpoint,
) -> None:
    archive = runtime_session.archive
    if not isinstance(archive, PostgresArtifactStore):
        raise TypeError("checkpoint benchmark requires PostgreSQL artifacts")
    artifacts = committed.installed.prepared.materialization.artifacts
    if not artifacts:
        raise RuntimeError("preferred checkpoint has no independently stored artifacts")
    for artifact in artifacts:
        semantic_metadata_fingerprint = artifact.semantic_metadata.get(
            "semantic_metadata_fingerprint"
        )
        if not isinstance(semantic_metadata_fingerprint, str):
            raise RuntimeError("checkpoint artifact lacks semantic metadata identity")
        deleted = archive.delete_if_identity(
            artifact.artifact_id,
            session_id=runtime_session.runtime_session_id,
            digest=f"sha256:{sha256(artifact.canonical_bytes).hexdigest()}",
            media_type=artifact.media_type,
            semantic_metadata_fingerprint=semantic_metadata_fingerprint,
        )
        if not deleted:
            raise RuntimeError(
                f"checkpoint artifact disappeared before controlled deletion: "
                f"{artifact.artifact_id}"
            )


async def _append_model_step(
    *,
    runtime_session: RuntimeSession,
    run: BenchmarkContextRun,
    llm_config: LLMConfig,
    call_ordinal: int,
    step: _TrajectoryStep,
) -> None:
    step_context = EventContext(
        run_id=run.event_context.run_id,
        turn_id=f"turn:benchmark:{_hex_identity(f'{run.event_context.run_id}:turn:{call_ordinal}')}",
        reply_id=f"reply:benchmark:{_hex_identity(f'{run.event_context.run_id}:reply:{call_ordinal}')}",
    )
    run.state.turn_id = step_context.turn_id
    run.state.reply_id = step_context.reply_id
    registry = LLMTransportRegistry(production_mode=True)
    registry.register(
        SanitizingLLMTransport(
            DeterministicContextStreamTransport(
                call_ordinal=call_ordinal,
                semantic_delta_events=step.semantic_delta_events,
                characters_per_delta=step.characters_per_delta,
                tool_call_count=len(step.tool_result_characters),
            )
        )
    )
    runtime = LLMRuntime(config=llm_config, registry=registry)
    call_target = runtime.resolve_target(role=ModelRole.PRO)
    if call_target.fact != run.target.fact:
        raise RuntimeError("benchmark call target drifted from RunStart target")
    call_id = _resolved_call_id(
        f"{run.event_context.run_id}:benchmark:{call_ordinal}"
    )
    call = ResolvedModelCall(
        target=call_target,
        fact=ResolvedModelCallFact(
            resolved_model_call_id=call_id,
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
            context_mode=ModelContextMode.COMPILED,
            target=call_target.fact,
        ),
    )
    context = LLMContext(
        messages=(LLMMessage.user(f"benchmark step {call_ordinal}"),),
        context_id=f"context:fixture:{run.event_context.run_id}:{call_ordinal}",
        resolved_model_call_id=call_id,
        target_fingerprint=run.target.fact.target_fingerprint,
        model_call_index=call_ordinal,
        tools=tuple(
            ToolSpec(
                name=f"benchmark_tool_{tool_ordinal}",
                description="Deterministic benchmark tool.",
                parameters={
                    "type": "object",
                    "properties": {
                        "call_ordinal": {"type": "integer"},
                        "tool_ordinal": {"type": "integer"},
                    },
                    "required": ["call_ordinal", "tool_ordinal"],
                    "additionalProperties": False,
                },
            )
            for tool_ordinal in range(
                1,
                len(step.tool_result_characters) + 1,
            )
        ),
    )
    context = replace(
        context,
        compiler_estimated_input_tokens=(
            call_target.token_estimator.estimate_context(context).total_input_tokens
        ),
    )
    await _advance_rollout_phase_for_call(
        runtime_session=runtime_session,
        run=run,
        event_context=step_context,
        call=call,
    )
    start_bundle = prepare_model_lifecycle_start_bundle(
        call=call,
        context=context,
        event_context=step_context,
        runtime_session=runtime_session,
        lifecycle_kind="main_assistant_reply",
        run_execution_activation=run.activation,
    )
    handle = runtime.start_stream(
        call=call,
        context=context,
        event_context=step_context,
        start_bundle=start_bundle,
        commit_port=RuntimeSessionModelStreamEventCommitPort(
            runtime_session=runtime_session,
            state=run.state,
        ),
        execution_registry=runtime_session.model_stream_execution_registry,
    )
    result = await handle.wait_result()
    expected_tool_call_ids = tuple(
        _tool_call_id(call_ordinal, tool_ordinal)
        for tool_ordinal in range(1, len(step.tool_result_characters) + 1)
    )
    actual_tool_call_ids = tuple(item.tool_call_id for item in result.tool_calls)
    if actual_tool_call_ids != expected_tool_call_ids:
        raise RuntimeError(
            "benchmark model tool calls drifted: "
            f"expected={expected_tool_call_ids!r}, actual={actual_tool_call_ids!r}"
        )
    owner = run.working_set.model_call_control_owner
    if owner is None:
        raise RuntimeError("benchmark model step lacks a control owner")
    await owner.resolve_completed_call(
        result=result,
        model_call_index=call_ordinal,
        event_context=step_context,
        runtime_session=runtime_session,
        state=run.state,
    )
    for tool_ordinal, result_characters in enumerate(
        step.tool_result_characters,
        start=1,
    ):
        tool_call_id = _tool_call_id(call_ordinal, tool_ordinal)
        events = build_tool_result_error_events(
            step_context,
            tool_call_id=tool_call_id,
            tool_call_name=f"benchmark_tool_{tool_ordinal}",
            message=_tool_result_text(
                call_ordinal=call_ordinal,
                tool_ordinal=tool_ordinal,
                characters=result_characters,
            ),
            state=ToolResultState.SUCCESS,
            semantics=build_unknown_result_semantics(
                result_state=ToolResultStateFact("success")
            ),
        )
        await runtime_session.emit_many(events, state=run.state)
    if step.tool_result_characters:
        await _ingest_tool_result_projections(
            runtime_session=runtime_session,
            run=run,
            resolved_call=call,
            event_context=step_context,
        )


async def _ingest_tool_result_projections(
    *,
    runtime_session: RuntimeSession,
    run: BenchmarkContextRun,
    resolved_call: ResolvedModelCall,
    event_context: EventContext,
) -> None:
    projection_input = await prepare_live_transcript_projection(
        runtime_session=runtime_session,
        working_set=run.working_set,
        budget=LoopBudget(),
    )
    rendered = render_prepared_tool_result_units(
        prepared=projection_input.prepared_tool_results,
        transcript=projection_input.normalized_transcript.transcript,
        token_estimator=resolved_call.target.token_estimator,
    )
    store = runtime_session.long_horizon_state_store
    window_state = store.window_state(run.event_context.run_id)
    if window_state is None or window_state.active_window_id is None:
        raise RuntimeError("benchmark projection ingest lacks an active window")
    window = window_state.windows[window_state.active_window_id]
    current = store.projection_state(window.window_id)
    if current is None:
        raise RuntimeError("benchmark projection ingest lost its baseline")
    planning_input = prepare_current_run_projection_planning_input(
        run_id=run.event_context.run_id,
        run_start_sequence=run.working_set.run_start_sequence,
        window=window,
        current_projection=current,
        canonical_slice=projection_input.authority_slice,
        transcript=projection_input.normalized_transcript.transcript,
        tool_result_units=projection_input.normalized_transcript.tool_result_units,
        context_budget=resolved_call.target.context_budget,
        allocation_policy=run.working_set.long_horizon_contract.window_policy,
        estimator=resolved_call.target.fact.token_estimator,
        pending_interaction=False,
        tool_call_in_flight=False,
    )
    plan = plan_new_result_ingest(
        event_context=event_context,
        window=window,
        current_state=current,
        units=projection_input.normalized_transcript.tool_result_units,
        rendered=rendered,
        token_estimator=resolved_call.target.token_estimator,
        policy=planning_input.allocation_policy,
        protection_facts=planning_input.protection_facts,
        source_through_sequence=projection_input.authority_slice.through_sequence,
    )
    if plan is None:
        return
    stored = tuple(await runtime_session.emit_many(plan.events, state=run.state))
    if tuple(item.id for item in stored) != tuple(item.id for item in plan.events):
        raise RuntimeError("benchmark projection ingest committed unexpected facts")
    if store.projection_state(window.window_id) != plan.final_state:
        raise RuntimeError("benchmark projection ingest reducer differs from plan")


async def _advance_rollout_phase_for_call(
    *,
    runtime_session: RuntimeSession,
    run: BenchmarkContextRun,
    event_context: EventContext,
    call: ResolvedModelCall,
) -> None:
    for _ in range(4):
        binding = resolve_run_rollout_binding(
            runtime_session,
            run_id=run.event_context.run_id,
        )
        if binding.child_state is not None:
            raise RuntimeError("root context benchmark resolved a child rollout")
        quote = calculate_model_call_reservation(
            target=call.target.fact,
            resolved_model_call_id=call.fact.resolved_model_call_id,
            policy=binding.account.policy,
        )
        plan = plan_root_model_admission(
            account=binding.account,
            state=binding.parent_state,
            quote=quote,
            purpose=call.fact.purpose,
        )
        if plan.action == "admit":
            return
        if plan.action == "transition":
            await runtime_session.emit(
                build_rollout_phase_transition_event(
                    event_context=event_context,
                    account=binding.account,
                    state=binding.parent_state,
                    plan=plan,
                ),
                state=run.state,
            )
            continue
        raise RuntimeError(
            "benchmark trajectory exceeds its resolved rollout contract: "
            f"{plan.action}"
        )
    raise RuntimeError("benchmark rollout phase did not converge")


async def _measure_compile_point(
    *,
    runtime_session: RuntimeSession,
    run: BenchmarkContextRun,
    call_ordinal: int,
    point_id: str,
) -> ContextCompilePointObservation:
    return (
        await _prepare_compile_point(
            runtime_session=runtime_session,
            run=run,
            call_ordinal=call_ordinal,
            point_id=point_id,
        )
    ).observation


async def _prepare_compile_point(
    *,
    runtime_session: RuntimeSession,
    run: BenchmarkContextRun,
    call_ordinal: int,
    point_id: str,
) -> _PreparedCompilePoint:
    compile_call_id = _resolved_call_id(
        f"{run.event_context.run_id}:compile:{point_id}"
    )
    resolved_call = ResolvedModelCall(
        target=run.target,
        fact=ResolvedModelCallFact(
            resolved_model_call_id=compile_call_id,
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
            context_mode=ModelContextMode.COMPILED,
            target=run.target.fact,
        ),
    )
    exposure = run.working_set.effective_exposure_plan
    if exposure is None:
        raise RuntimeError("benchmark context preparation lacks capability exposure")
    started = perf_counter()
    prepared = await prepare_live_context_snapshot(
        runtime_session=runtime_session,
        working_set=run.working_set,
        resolved_call=resolved_call,
        budget=LoopBudget(),
        system_prompt="Pulsara deterministic context benchmark.",
        context_id=f"context:benchmark:{run.event_context.run_id}:{point_id}",
        model_call_index=call_ordinal + 1,
        compile_attempt_index=1,
        context_retry_index=0,
        compiled_at_utc="2026-01-01T00:00:00.000000Z",
        workspace_kind="project",
        terminal_current_cwd=str(runtime_session.workspace_root),
        session_timezone="UTC",
        compiled_local_date="2026-01-01",
        candidate_sources=ContextCandidateCollectionInput(
            system_prompt="Pulsara deterministic context benchmark.",
            capability_catalog=exposure.catalog_prompt,
            capability_active_skill=exposure.active_skill_prompt,
        ),
    )
    prepare_seconds = perf_counter() - started
    compile_started = perf_counter()
    rendered = render_prepared_tool_result_units(
        prepared=prepared.prepared_tool_results,
        transcript=prepared.normalized_transcript.transcript,
        token_estimator=resolved_call.target.token_estimator,
        projection_state=prepared.projection_state,
    )
    compiled = compile_context_from_facts(
        facts=prepared.invocation,
        transcript=prepared.normalized_transcript.transcript,
        rendered_tool_results=rendered,
        prepared_rollups=(),
        section_candidates=prepared.prepared_candidates,
    )
    compile_seconds = perf_counter() - compile_started
    evidence = prepared.transcript_projection_evidence
    projection_base = evidence.projection_base
    projection_base_id = (
        projection_base.checkpoint_acceleration.checkpoint_id
        if isinstance(projection_base, CheckpointProjectionBaseFact)
        else projection_base.common.run_seed_reference.seed_artifact_id
    )
    projection_base_fingerprint = context_fingerprint(
        "benchmark-transcript-projection-base:v1",
        projection_base,
    )
    authority_plan = prepared.snapshot_build_input.authority_slice_plan
    terminal_document_count = len(
        stable_entry_projection_references(evidence.stable_entries)
    )
    terminal_projection_source_delta_count = 0
    artifact_backed_terminal_content_count = 0
    for reference in stable_entry_projection_references(
        evidence.stable_entries
    ):
        document = evidence.document_registry.resolve(reference)
        source_fact = document.source_fact
        terminal_projection_source_delta_count += (
            source_fact.source_semantic_item_count
            if isinstance(source_fact, ModelCallSemanticSourceFact)
            else source_fact.source_delta_count
        )
        if not isinstance(document.payload, ToolTerminalProjectionPayloadFact):
            continue
        artifact_backed_terminal_content_count += sum(
            isinstance(item.content, TerminalArtifactContentReferenceFact)
            for item in document.payload.canonical_result_block.content_blocks
        )
    active_window = prepared.active_window
    subagent_selection = next(
        item
        for item in prepared.invocation.fact.candidate_source_selections
        if item.source_instance_id == "subagent:results"
    )
    observation = ContextCompilePointObservation(
        point_id=point_id,
        context_prepare_wall_seconds=prepare_seconds,
        context_compile_wall_seconds=compile_seconds,
        source_through_sequence=prepared.authority_slice.through_sequence,
        authority_event_count=len(prepared.authority_slice.events),
        semantic_delta_event_count=len(evidence.semantic_delta_events),
        stable_entry_count=len(evidence.stable_entries),
        normalized_message_count=len(
            prepared.normalized_transcript.transcript.messages
        ),
        normalized_tool_result_count=len(
            prepared.normalized_transcript.tool_result_units
        ),
        projection_base_fingerprint=projection_base_fingerprint,
        projection_base_kind=projection_base.base_kind,
        projection_base_id=projection_base_id,
        active_window_id=active_window.window_id,
        active_window_generation=active_window.generation,
        source_summary_artifact_id=active_window.source_summary_artifact_id,
        semantic_source_fingerprint=(
            evidence.semantic_source.semantic_source_fingerprint
        ),
        authority_plan_fingerprint=authority_plan.plan_fingerprint,
        normalized_transcript_fingerprint=(
            prepared.normalized_transcript.transcript.transcript_fingerprint
        ),
        provider_payload_fingerprint=provider_neutral_payload_fingerprint(
            compiled.llm_context
        ),
        terminal_document_count=terminal_document_count,
        terminal_projection_source_delta_count=(
            terminal_projection_source_delta_count
        ),
        artifact_backed_terminal_content_count=(
            artifact_backed_terminal_content_count
        ),
        max_stable_entry_bytes=max(
            (
                len(canonical_json_bytes(item.model_dump(mode="json")))
                for item in evidence.stable_entries
            ),
            default=0,
        ),
        selected_subagent_result_count=len(
            subagent_selection.selected_source_ids
        ),
        subagent_graph_semantic_fingerprint=(
            prepared.invocation.fact.subagent_graph_semantic_source.semantic_source_fingerprint
        ),
    )
    return _PreparedCompilePoint(
        observation=observation,
        resolved_call=resolved_call,
        prepared=prepared,
        rendered=rendered,
        compiled=compiled,
    )


async def _stabilize_acceleration(runtime_session: RuntimeSession) -> None:
    """Move acceleration-only graph state outside the measured context call."""

    for _ in range(4):
        before = runtime_session.event_log.next_sequence() - 1
        await runtime_session.subagent_graph_checkpoint_service.restore_for_selection(
            requested_through_sequence=before
        )
        await runtime_session.subagent_graph_checkpoint_service.drain_pending(
            deadline_monotonic=monotonic() + 30.0
        )
        after = runtime_session.event_log.next_sequence() - 1
        if after == before:
            return
    raise RuntimeError("benchmark acceleration checkpoint did not quiesce")


def _trajectory_steps(
    scenario: SupportedContextScenario,
) -> tuple[tuple[_TrajectoryStep, ...], int]:
    if isinstance(scenario, LongPlanPrefixGrowthScenario):
        profile = scenario.ledger.tool_result_profile
        result_ordinal = 0
        steps: list[_TrajectoryStep] = []
        for delta_count, tool_count in zip(
            scenario.ledger.semantic_delta_events_per_call,
            scenario.ledger.tool_calls_per_model_call,
            strict=True,
        ):
            result_sizes: list[int] = []
            for _ in range(tool_count):
                result_ordinal += 1
                result_sizes.append(
                    profile.large_characters
                    if result_ordinal % profile.large_every_nth_result == 0
                    else profile.default_characters
                )
            steps.append(
                _TrajectoryStep(
                    semantic_delta_events=delta_count,
                    characters_per_delta=(
                        scenario.ledger.characters_per_semantic_delta
                    ),
                    tool_result_characters=tuple(result_sizes),
                )
            )
        return tuple(steps), 0
    if isinstance(scenario, IncrementalActiveWindowScenario):
        return (
            tuple(
                _TrajectoryStep(
                    semantic_delta_events=(
                        scenario.ledger.semantic_delta_events_per_call
                    ),
                    characters_per_delta=(
                        scenario.ledger.characters_per_semantic_delta
                    ),
                    tool_result_characters=(
                        (scenario.ledger.tool_result_characters,)
                        if call_ordinal
                        % scenario.ledger.tool_calls_every_nth_model_call
                        == 0
                        else ()
                    ),
                )
                for call_ordinal in range(
                    1,
                    scenario.ledger.model_calls + 1,
                )
            ),
            0,
        )
    if isinstance(scenario, ArtifactHeavyToolsScenario):
        return (
            tuple(
                _TrajectoryStep(
                    semantic_delta_events=(
                        scenario.ledger.semantic_delta_events_per_call
                    ),
                    characters_per_delta=(
                        scenario.ledger.characters_per_semantic_delta
                    ),
                    tool_result_characters=(
                        (scenario.ledger.tool_results.canonical_result_characters,)
                        * scenario.ledger.tool_calls_per_model_call
                    ),
                )
                for _ in range(scenario.ledger.model_calls)
            ),
            scenario.compile_schedule.repeat_last_compile,
        )
    raise TypeError(f"unsupported context scenario: {scenario.scenario_id}")


def _runtime_session(
    *,
    dsn: str,
    workspace_root: Path,
    runtime_session_id: str,
) -> RuntimeSession:
    return RuntimeSession(
        workspace_root,
        event_log=PostgresEventLog(
            dsn=dsn,
            runtime_session_id=runtime_session_id,
            workspace_root=workspace_root,
        ),
        archive=PostgresArtifactStore(dsn),
        tool_result_artifacts=PostgresToolResultArtifactIndex(dsn),
        runtime_session_id=runtime_session_id,
    )


def _benchmark_llm_config(
    *,
    default_output_tokens: int = 512,
) -> LLMConfig:
    limits = ModelContextLimits(
        total_context_tokens=256_000,
        max_input_tokens=256_000,
        max_output_tokens=4_096,
        default_output_tokens=default_output_tokens,
        input_safety_margin_tokens=8_192,
    )
    slot = ModelSlotConfig(
        model_id="benchmark-model",
        limits=limits,
    )
    return LLMConfig(
        api_key="benchmark-no-network",
        base_url="https://benchmark.invalid/v1",
        pro=slot,
        flash=slot,
        api="mock",
        provider="benchmark",
        provider_profile=ProviderProfile(id="benchmark", wire_api="mock"),
    )


def _empty_transport_registry() -> LLMTransportRegistry:
    registry = LLMTransportRegistry(production_mode=True)
    registry.register(
        SanitizingLLMTransport(
            DeterministicContextStreamTransport(
                call_ordinal=0,
                semantic_delta_events=1,
                characters_per_delta=8,
                tool_call_count=0,
            )
        )
    )
    return registry


def _window_summary_json(
    *,
    source_entry_id: str,
    target_characters: int,
) -> str:
    payload = {
        "observed_facts": ["s"],
        "model_inferences": [],
        "unresolved_questions": ["Continue the benchmark task."],
        "critical_constraints": ["Preserve deterministic source attribution."],
        "artifact_locators": [],
        "cited_source_entry_ids": [source_entry_id],
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded) > target_characters:
        raise ValueError("configured compaction summary is smaller than its schema")
    payload["observed_facts"] = [
        "s" * (1 + target_characters - len(encoded))
    ]
    result = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(result) != target_characters:
        raise RuntimeError("deterministic summary character target drifted")
    return result


def _even_partition(total: int, count: int) -> tuple[int, ...]:
    if total < count or count <= 0:
        raise ValueError("benchmark partition requires one or more items per bucket")
    quotient, remainder = divmod(total, count)
    return tuple(
        quotient + (1 if ordinal < remainder else 0)
        for ordinal in range(count)
    )


def _fixed_width_ascii(call_ordinal: int, index: int, width: int) -> str:
    prefix = f"{call_ordinal:x}:{index:x}:"
    if len(prefix) > width:
        raise ValueError("benchmark counter exceeded its fixed-width contract")
    return prefix + ("x" * (width - len(prefix)))


def _tool_call_id(call_ordinal: int, tool_ordinal: int) -> str:
    return f"benchmark-call:{call_ordinal}:tool:{tool_ordinal}"


def _tool_result_text(
    *,
    call_ordinal: int,
    tool_ordinal: int,
    characters: int,
) -> str:
    sentinel = f"result:{call_ordinal}:{tool_ordinal}:"
    if len(sentinel) > characters:
        raise ValueError("tool result sentinel exceeds requested payload")
    return sentinel + ("r" * (characters - len(sentinel)))


def _hex_identity(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:32]


def _resolved_call_id(value: str) -> str:
    return f"model_call:{_hex_identity(value)}"


__all__ = [
    "ContextCompilePointObservation",
    "ContextPreparationObservation",
    "run_context_preparation_sample",
]
