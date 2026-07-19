"""Deterministic production-path benchmark for append-only provider input."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from time import monotonic, perf_counter
from typing import AsyncIterator

from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.event import (
    ModelCallStartEvent,
    ProviderInputAppendCommittedEvent,
    ProviderInputGenerationRolloverResolvedEvent,
)
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.llm import LLMRuntime
from pulsara_agent.llm.raw_provider import (
    RawProviderBlockEnd,
    RawProviderBlockStart,
    RawProviderStreamItem,
    RawProviderTextDelta,
    RawProviderToolCallDelta,
)
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.primitives.context import canonical_json_bytes, context_fingerprint
from pulsara_agent.runtime.agent import AgentRuntime
from pulsara_agent.runtime.context_input.io_service import ContextInputIoService
from pulsara_agent.runtime.provider_input.materialization import (
    message_semantic_fingerprint,
    tool_semantic_fingerprint,
)
from pulsara_agent.runtime.provider_input.vector import load_provider_input_vector
from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.runtime.tool_artifacts import InMemoryToolResultArtifactIndex
from tests.support import run_agent_task, test_llm_config, test_model_limits


@dataclass(frozen=True, slots=True)
class ProviderInputPrefixBenchmarkResult:
    requested_model_calls: int
    observed_model_calls: int
    generation_count: int
    model_call_generation_ids: tuple[str, ...]
    calls_per_generation: dict[str, int]
    generation_revisions: tuple[int, ...]
    retained_prefix_estimated_tokens: tuple[int, ...]
    new_append_estimated_tokens: tuple[int, ...]
    provider_input_lcp_units: tuple[int, ...]
    same_generation_prefix_comparison_count: int
    old_transcript_unit_rerender_count: int
    rollover_count: int
    rollover_reasons: tuple[str, ...]
    context_prepare_wall_seconds: float
    context_io_wall_seconds: float
    context_prepare_non_io_seconds: float
    provider_input_artifact_bytes: int
    context_manifest_artifact_bytes: int
    max_model_start_bytes: int
    max_horizon_root_bytes: int
    ledger_event_count: int
    exact_restore_wall_seconds: float
    exact_restore_unit_count: int
    prefix_invariant_holds: bool
    benchmark_wall_seconds: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class DeterministicProviderInputTransport:
    api = "provider-input-prefix-benchmark"
    binding_id = "benchmark.provider-input-prefix"
    contract_version = "v1"

    def __init__(self, *, model_calls: int) -> None:
        if model_calls < 2:
            raise ValueError("provider input prefix benchmark needs at least two calls")
        self._model_calls = model_calls
        self.contexts: list[LLMContext] = []

    async def stream(
        self,
        *,
        call,
        context: LLMContext,
        event_context,
    ) -> AsyncIterator[RawProviderStreamItem]:
        del call, event_context
        self.contexts.append(context)
        ordinal = len(self.contexts)
        if ordinal < self._model_calls:
            tool_call_id = f"benchmark-prefix-tool:{ordinal}"
            yield RawProviderBlockStart(
                block_kind="tool_call",
                block_id=tool_call_id,
                tool_call_name="unknown_contract_tool",
            )
            yield RawProviderToolCallDelta(
                tool_call_id=tool_call_id,
                delta="{}",
            )
            yield RawProviderBlockEnd(
                block_kind="tool_call",
                block_id=tool_call_id,
            )
            return
        block_id = f"benchmark-prefix-text:{ordinal}"
        yield RawProviderBlockStart(block_kind="text", block_id=block_id)
        yield RawProviderTextDelta(
            block_id=block_id,
            delta="PULSARA_PROVIDER_PREFIX_BENCHMARK_OK",
        )
        yield RawProviderBlockEnd(block_kind="text", block_id=block_id)


async def run_provider_input_prefix_benchmark(
    *,
    workspace_root: Path,
    model_calls: int = 4,
) -> ProviderInputPrefixBenchmarkResult:
    """Run a fixed multi-call tool trajectory through the production agent loop."""

    workspace_root.mkdir(parents=True, exist_ok=True)
    archive = InMemoryArchiveStore()
    runtime_session = RuntimeSession(
        workspace_root,
        event_log=InMemoryEventLog(),
        archive=archive,
        tool_result_artifacts=InMemoryToolResultArtifactIndex(),
        runtime_session_id="runtime:provider-input-prefix-benchmark",
    )
    transport = DeterministicProviderInputTransport(model_calls=model_calls)
    registry = LLMTransportRegistry()
    registry.register(transport)
    # The default test target reserves a 64K input safety margin, which leaves
    # enough worst-case rollout headroom for only four model calls. This
    # benchmark needs to exercise the Call-5 prefix without bypassing durable
    # rollout admission, so it freezes an otherwise identical target contract
    # with no additional test-only margin.
    benchmark_limits = test_model_limits(input_safety_margin_tokens=0)
    config = test_llm_config(
        api_key="benchmark-no-network",
        base_url="https://benchmark.invalid/v1",
        pro_model="benchmark-pro",
        flash_model="benchmark-flash",
        api=transport.api,
        pro_limits=benchmark_limits,
        flash_limits=benchmark_limits,
    )
    llm_runtime = LLMRuntime(config=config, registry=registry)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=llm_runtime,
    )

    import pulsara_agent.runtime.agent as agent_module

    prepare_seconds: list[float] = []
    io_seconds: list[float] = []
    original_prepare = agent_module.prepare_live_context_snapshot
    original_io_execute = ContextInputIoService.execute

    async def timed_prepare(*args, **kwargs):
        started = perf_counter()
        try:
            return await original_prepare(*args, **kwargs)
        finally:
            prepare_seconds.append(perf_counter() - started)

    async def timed_io_execute(self, *args, **kwargs):
        started = perf_counter()
        try:
            return await original_io_execute(self, *args, **kwargs)
        finally:
            io_seconds.append(perf_counter() - started)

    agent_module.prepare_live_context_snapshot = timed_prepare
    ContextInputIoService.execute = timed_io_execute
    started = perf_counter()
    try:
        result = await run_agent_task(
            agent,
            "Exercise a deterministic multi-call provider-prefix trajectory.",
        )
    finally:
        agent_module.prepare_live_context_snapshot = original_prepare
        ContextInputIoService.execute = original_io_execute
    wall_seconds = perf_counter() - started
    if result.final_text != "PULSARA_PROVIDER_PREFIX_BENCHMARK_OK":
        raise RuntimeError("deterministic provider-prefix trajectory did not finish")

    events = tuple(runtime_session.event_log.iter())
    starts = tuple(event for event in events if isinstance(event, ModelCallStartEvent))
    appends = tuple(
        event
        for event in events
        if isinstance(event, ProviderInputAppendCommittedEvent)
    )
    rollovers = tuple(
        event
        for event in events
        if isinstance(event, ProviderInputGenerationRolloverResolvedEvent)
    )
    if len(starts) != model_calls or len(appends) != model_calls:
        raise RuntimeError("provider-prefix benchmark model lifecycle count drifted")

    target = agent.resolve_run_model_target()
    context_estimates = tuple(
        target.token_estimator.estimate_context(context).total_input_tokens
        for context in transport.contexts
    )
    generation_ids = tuple(event.generation_id for event in appends)
    retained = tuple(
        context_estimates[index - 1]
        if index > 0 and generation_ids[index - 1] == generation_ids[index]
        else 0
        for index in range(len(context_estimates))
    )
    appended_tokens = tuple(
        total if retained[index] == 0 else max(0, total - context_estimates[index - 1])
        for index, total in enumerate(context_estimates)
    )
    flattened = tuple(
        _context_semantic_units(context) for context in transport.contexts
    )
    lcp_units = tuple(
        0 if index == 0 else _longest_common_prefix(flattened[index - 1], current)
        for index, current in enumerate(flattened)
    )
    same_generation_pairs = tuple(
        index
        for index in range(1, len(generation_ids))
        if generation_ids[index - 1] == generation_ids[index]
    )
    provider_prefix_ok = all(
        flattened[index - 1] == flattened[index][: len(flattened[index - 1])]
        for index in same_generation_pairs
    )
    restored_vectors = tuple(
        load_provider_input_vector(
            archive=archive,
            runtime_session_id=runtime_session.runtime_session_id,
            root=event.resulting_core_state.unit_vector_root,
            deadline_monotonic=monotonic() + 30.0,
        )[0]
        for event in appends
    )
    vector_semantics = tuple(
        tuple(item.attribution.semantic.semantic_fingerprint for item in units)
        for units in restored_vectors
    )
    vector_prefix_ok = all(
        vector_semantics[index - 1]
        == vector_semantics[index][: len(vector_semantics[index - 1])]
        for index in same_generation_pairs
    )
    seen_transcript_owners: dict[str, Counter[str]] = defaultdict(Counter)
    duplicate_renders = 0
    for index, units in enumerate(restored_vectors):
        previous_count = (
            len(restored_vectors[index - 1])
            if index > 0 and generation_ids[index - 1] == generation_ids[index]
            else 0
        )
        for unit in units[previous_count:]:
            if unit.attribution.semantic.unit_kind != "transcript_message":
                continue
            owner = unit.attribution.owner_semantic_fingerprint
            duplicate_renders += seen_transcript_owners[generation_ids[index]][owner]
            seen_transcript_owners[generation_ids[index]][owner] += 1
    prefix_ok = provider_prefix_ok and vector_prefix_ok
    calls_by_generation = Counter(
        start.provider_input_reference.generation_id
        for start in starts
        if start.provider_input_reference is not None
    )
    provider_bytes = sum(
        blob.size_bytes
        for blob_id, blob in archive.blobs.items()
        if blob_id.startswith("provider-input-")
    )
    manifest_bytes = sum(
        blob.size_bytes
        for blob_id, blob in archive.blobs.items()
        if blob_id.startswith("context-input-manifest")
    )
    start_bytes = tuple(
        len(canonical_json_bytes(item.model_dump(mode="json"))) for item in starts
    )
    horizon_bytes = tuple(
        len(
            canonical_json_bytes(
                item.provider_input_reference.authority_horizon_set.model_dump(
                    mode="json"
                )
            )
        )
        for item in starts
        if item.provider_input_reference is not None
    )

    final_root = appends[-1].resulting_core_state.unit_vector_root
    runtime_session.provider_input_generation_store.clear_resident_cache()
    restore_started = perf_counter()
    restored_units, _reachable = load_provider_input_vector(
        archive=archive,
        runtime_session_id=runtime_session.runtime_session_id,
        root=final_root,
        deadline_monotonic=monotonic() + 30.0,
    )
    restore_seconds = perf_counter() - restore_started
    total_prepare = sum(prepare_seconds)
    total_io = sum(io_seconds)
    benchmark = ProviderInputPrefixBenchmarkResult(
        requested_model_calls=model_calls,
        observed_model_calls=len(starts),
        generation_count=len(calls_by_generation),
        model_call_generation_ids=generation_ids,
        calls_per_generation=dict(sorted(calls_by_generation.items())),
        generation_revisions=tuple(event.resulting_revision for event in appends),
        retained_prefix_estimated_tokens=retained,
        new_append_estimated_tokens=appended_tokens,
        provider_input_lcp_units=lcp_units,
        same_generation_prefix_comparison_count=len(same_generation_pairs),
        old_transcript_unit_rerender_count=duplicate_renders,
        rollover_count=len(rollovers),
        rollover_reasons=tuple(
            item.rollover_request.intent.reason.value for item in rollovers
        ),
        context_prepare_wall_seconds=total_prepare,
        context_io_wall_seconds=total_io,
        context_prepare_non_io_seconds=max(0.0, total_prepare - total_io),
        provider_input_artifact_bytes=provider_bytes,
        context_manifest_artifact_bytes=manifest_bytes,
        max_model_start_bytes=max(start_bytes, default=0),
        max_horizon_root_bytes=max(horizon_bytes, default=0),
        ledger_event_count=len(events),
        exact_restore_wall_seconds=restore_seconds,
        exact_restore_unit_count=len(restored_units),
        prefix_invariant_holds=prefix_ok,
        benchmark_wall_seconds=wall_seconds,
    )
    runtime_session.close()
    if not prefix_ok or duplicate_renders != 0:
        raise RuntimeError(
            "append-only provider-prefix benchmark invariant failed: "
            f"prefix_ok={prefix_ok}, duplicate_renders={duplicate_renders}, "
            f"lcp_units={lcp_units}, unit_counts="
            f"{tuple(len(item) for item in flattened)}, generations="
            f"{dict(calls_by_generation)}, rollovers="
            f"{tuple(item.rollover_request.intent.reason.value for item in rollovers)}"
        )
    return benchmark


def _context_semantic_units(context: LLMContext) -> tuple[str, ...]:
    units: list[str] = []
    if context.system_prompt is not None:
        units.append(
            context_fingerprint("benchmark-provider-system:v1", context.system_prompt)
        )
    units.extend(tool_semantic_fingerprint(item) for item in context.tools)
    units.extend(message_semantic_fingerprint(item) for item in context.messages)
    return tuple(units)


def _longest_common_prefix(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    count = 0
    for left_item, right_item in zip(left, right):
        if left_item != right_item:
            break
        count += 1
    return count


__all__ = [
    "ProviderInputPrefixBenchmarkResult",
    "run_provider_input_prefix_benchmark",
]
