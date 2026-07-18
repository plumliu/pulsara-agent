"""Production-path generator for the model semantic batching benchmark."""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import AsyncIterator

import psycopg

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    ModelCallEndEvent,
    PhysicalOperationReservationCreatedEvent,
    PhysicalOperationReservationSettledEvent,
    RolloutBudgetReservationSettledEvent,
    TextBlockSegmentEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
)
from pulsara_agent.event_log import PostgresEventLog
from pulsara_agent.llm.commit import (
    RuntimeSessionModelStreamEventCommitPort,
)
from pulsara_agent.llm.config import (
    DEFAULT_MODEL_CONTEXT_LIMITS,
    LLMConfig,
    ModelSlotConfig,
)
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.lifecycle import prepare_model_lifecycle_start_bundle
from pulsara_agent.llm.models import ModelRole
from pulsara_agent.llm.provider import ProviderProfile
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.resolution import ResolvedModelCall
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.llm.runtime import LLMRuntime
from pulsara_agent.llm.raw_provider import (
    RawLLMTransport,
    RawProviderBlockEnd,
    RawProviderBlockStart,
    RawProviderStreamItem,
    RawProviderTextDelta,
)
from pulsara_agent.llm.sanitizing_transport import SanitizingLLMTransport
from pulsara_agent.memory.artifacts.postgres_archive import (
    PostgresArtifactStore,
)
from pulsara_agent.primitives.context import canonical_json_bytes
from pulsara_agent.primitives.model_call import (
    CommittedModelCallResult,
    ModelCallPurpose,
    ModelContextMode,
    ResolvedModelCallFact,
    sha256_fingerprint,
)
from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.runtime.tool_artifacts import (
    PostgresToolResultArtifactIndex,
)

from generators.runtime_fixture import bootstrap_benchmark_root_run
from runners.scenario_contracts import (
    ModelSemanticBatchMatrixScenario,
    SemanticBatchCase,
)


@dataclass(frozen=True, slots=True)
class ModelSemanticBatchObservation:
    case_id: str
    model_stream_wall_seconds: float
    start_commit_port_wall_seconds: float
    semantic_commit_port_wall_seconds: float
    terminal_commit_port_wall_seconds: float
    logical_semantic_batch_count: int
    semantic_batch_sizes: tuple[int, ...]
    source_item_count: int
    durable_text_segment_count: int
    ledger_event_delta: int
    ledger_candidate_payload_byte_delta: int
    postgres_cluster_wal_lsn_delta_bytes: int
    ordered_semantic_content_fingerprint: str
    raw_reference_semantic_content_fingerprint: str
    terminal_projection_content_fingerprint: str
    terminal_projection_semantic_fingerprint: str
    physical_settlement_valid: bool
    physical_charged_candidate_events: int
    physical_charged_candidate_payload_bytes: int
    physical_charged_wrapper_bytes: int
    physical_charged_bookkeeping_events: int
    physical_charged_bookkeeping_bytes: int
    physical_total_charged_events: int
    physical_total_charged_payload_bytes: int
    physical_released_events: int
    physical_released_payload_bytes: int
    accounted_writer_path_only: bool


@dataclass(frozen=True, slots=True)
class PhysicalSettlementObservation:
    valid: bool
    charged_candidate_events: int
    charged_candidate_payload_bytes: int
    charged_wrapper_bytes: int
    charged_bookkeeping_events: int
    charged_bookkeeping_bytes: int
    total_charged_events: int
    total_charged_payload_bytes: int
    released_events: int
    released_payload_bytes: int


class DeterministicTextStreamTransport(RawLLMTransport):
    api = "mock"
    binding_id = "pulsara.benchmark.deterministic-text-stream"
    contract_version = "v1"

    def __init__(
        self,
        *,
        delta_events: int,
        characters_per_delta: int,
    ) -> None:
        self._delta_events = delta_events
        self._characters_per_delta = characters_per_delta

    async def stream(
        self,
        *,
        call: ResolvedModelCall,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[RawProviderStreamItem | TransportUsageReport]:
        del call, context, event_context
        block_id = "benchmark-text-block"
        yield RawProviderBlockStart(
            block_kind="text",
            block_id=block_id,
        )
        for index in range(self._delta_events):
            payload = _fixed_width_ascii(index, self._characters_per_delta)
            yield RawProviderTextDelta(
                block_id=block_id,
                delta=payload,
            )
        yield RawProviderBlockEnd(
            block_kind="text",
            block_id=block_id,
        )


class RecordingModelStreamCommitPort(
    RuntimeSessionModelStreamEventCommitPort
):
    def __init__(self, *, runtime_session: RuntimeSession) -> None:
        super().__init__(runtime_session=runtime_session, state=None)
        self.start_commit_port_wall_seconds = 0.0
        self.semantic_commit_port_wall_seconds = 0.0
        self.terminal_commit_port_wall_seconds = 0.0
        self.semantic_batch_sizes: list[int] = []

    async def commit_start(self, candidates, *, guard):
        started = perf_counter()
        try:
            return await super().commit_start(candidates, guard=guard)
        finally:
            self.start_commit_port_wall_seconds += perf_counter() - started

    async def commit_semantic(self, candidates, *, guard, live_cursor):
        started = perf_counter()
        try:
            return await super().commit_semantic(
                candidates,
                guard=guard,
                live_cursor=live_cursor,
            )
        finally:
            self.semantic_batch_sizes.append(len(candidates))
            self.semantic_commit_port_wall_seconds += perf_counter() - started

    async def commit_terminal(self, candidates, *, guard, live_cursor):
        started = perf_counter()
        try:
            return await super().commit_terminal(
                candidates,
                guard=guard,
                live_cursor=live_cursor,
            )
        finally:
            self.terminal_commit_port_wall_seconds += perf_counter() - started


async def run_model_semantic_batch_sample(
    *,
    scenario: ModelSemanticBatchMatrixScenario,
    execution_case: SemanticBatchCase,
    dsn: str,
    workspace_root: Path,
    sample_identity: str,
) -> ModelSemanticBatchObservation:
    """Run one isolated sample through the real model/runtime/PostgreSQL path."""

    block = scenario.workload.model_stream.blocks[0]
    if block.kind != "text":
        raise ValueError("model semantic batching requires a text workload")
    runtime_session_id = f"runtime:benchmark:{_hex_identity(sample_identity)}"
    event_context = EventContext(
        run_id=f"run:benchmark:{_hex_identity(sample_identity + ':run')}",
        turn_id=f"turn:benchmark:{_hex_identity(sample_identity + ':turn')}",
        reply_id=f"reply:benchmark:{_hex_identity(sample_identity + ':reply')}",
    )
    event_log = PostgresEventLog(
        dsn=dsn,
        runtime_session_id=runtime_session_id,
        workspace_root=workspace_root,
    )
    runtime_session = RuntimeSession(
        workspace_root,
        event_log=event_log,
        archive=PostgresArtifactStore(dsn),
        tool_result_artifacts=PostgresToolResultArtifactIndex(dsn),
        runtime_session_id=runtime_session_id,
    )
    try:
        registry = LLMTransportRegistry(production_mode=True)
        registry.register(
            SanitizingLLMTransport(
                DeterministicTextStreamTransport(
                    delta_events=block.delta_events_per_block,
                    characters_per_delta=block.characters_per_delta,
                )
            )
        )
        config = _benchmark_llm_config()
        runtime = LLMRuntime(config=config, registry=registry)
        target = runtime.resolve_target(role=ModelRole.PRO)
        call_id = f"model_call:{_hex_identity(sample_identity + ':model-call')}"
        call = ResolvedModelCall(
            target=target,
            fact=ResolvedModelCallFact(
                resolved_model_call_id=call_id,
                purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
                context_mode=ModelContextMode.COMPILED,
                target=target.fact,
            ),
        )
        context = LLMContext(
            messages=(LLMMessage.user("durable runtime benchmark"),),
            context_id=f"context:{_hex_identity(sample_identity + ':context')}",
            resolved_model_call_id=call_id,
            target_fingerprint=target.fact.target_fingerprint,
            model_call_index=1,
        )
        context = replace(
            context,
            compiler_estimated_input_tokens=(
                target.token_estimator.estimate_context(context).total_input_tokens
            ),
        )
        activation = await bootstrap_benchmark_root_run(
            runtime_session,
            event_context=event_context,
            model_target=target.fact,
        )
        start_bundle = prepare_model_lifecycle_start_bundle(
            call=call,
            context=context,
            event_context=event_context,
            runtime_session=runtime_session,
            lifecycle_kind="main_assistant_reply",
            run_execution_activation=activation,
        )
        port = RecordingModelStreamCommitPort(runtime_session=runtime_session)
        before = event_log.read_ledger_usage_snapshot()
        wal_before = _current_wal_lsn(dsn)
        started = perf_counter()
        handle = runtime.start_stream(
            call=call,
            context=context,
            event_context=event_context,
            start_bundle=start_bundle,
            commit_port=port,
            execution_registry=runtime_session.model_stream_execution_registry,
        )
        completion = await handle.wait_completed()
        result = await handle.wait_result()
        model_stream_wall_seconds = perf_counter() - started
        wal_after = _current_wal_lsn(dsn)
        after = event_log.read_ledger_usage_snapshot()
        if completion.terminal_outcome != "completed":
            raise RuntimeError(
                f"benchmark model call did not complete: {completion.terminal_outcome}"
            )
        if result.terminal_outcome != "completed":
            raise RuntimeError("benchmark materialized model result did not complete")
        ledger_events = tuple(event_log.iter())
        raw_reference_content = "".join(
            _fixed_width_ascii(index, block.characters_per_delta)
            for index in range(block.delta_events_per_block)
        )
        text_segment_count = sum(
            isinstance(event, TextBlockSegmentEvent)
            for event in completion.committed_events
        )
        account = runtime_session.materialization_account_store.snapshot()
        physical_settlement = _physical_settlement_observation(ledger_events)
        return ModelSemanticBatchObservation(
            case_id=execution_case.case_id,
            model_stream_wall_seconds=model_stream_wall_seconds,
            start_commit_port_wall_seconds=(
                port.start_commit_port_wall_seconds
            ),
            semantic_commit_port_wall_seconds=(
                port.semantic_commit_port_wall_seconds
            ),
            terminal_commit_port_wall_seconds=(
                port.terminal_commit_port_wall_seconds
            ),
            logical_semantic_batch_count=len(port.semantic_batch_sizes),
            semantic_batch_sizes=tuple(port.semantic_batch_sizes),
            source_item_count=block.delta_events_per_block + 2,
            durable_text_segment_count=text_segment_count,
            ledger_event_delta=after.event_count - before.event_count,
            ledger_candidate_payload_byte_delta=(
                after.candidate_payload_bytes - before.candidate_payload_bytes
            ),
            postgres_cluster_wal_lsn_delta_bytes=_wal_bytes(
                dsn,
                wal_before,
                wal_after,
            ),
            ordered_semantic_content_fingerprint=(
                _ordered_semantic_content_fingerprint(
                    completion.committed_events
                )
            ),
            raw_reference_semantic_content_fingerprint=(
                _semantic_content_fingerprint(
                    block_id="benchmark-text-block",
                    content=raw_reference_content,
                )
            ),
            terminal_projection_content_fingerprint=(
                _semantic_content_fingerprint(
                    block_id="benchmark-text-block",
                    content=_terminal_projection_text(result),
                )
            ),
            terminal_projection_semantic_fingerprint=(
                _terminal_projection_semantic_fingerprint(
                    completion.committed_events
                )
            ),
            physical_settlement_valid=physical_settlement.valid,
            physical_charged_candidate_events=(
                physical_settlement.charged_candidate_events
            ),
            physical_charged_candidate_payload_bytes=(
                physical_settlement.charged_candidate_payload_bytes
            ),
            physical_charged_wrapper_bytes=(
                physical_settlement.charged_wrapper_bytes
            ),
            physical_charged_bookkeeping_events=(
                physical_settlement.charged_bookkeeping_events
            ),
            physical_charged_bookkeeping_bytes=(
                physical_settlement.charged_bookkeeping_bytes
            ),
            physical_total_charged_events=(
                physical_settlement.total_charged_events
            ),
            physical_total_charged_payload_bytes=(
                physical_settlement.total_charged_payload_bytes
            ),
            physical_released_events=physical_settlement.released_events,
            physical_released_payload_bytes=(
                physical_settlement.released_payload_bytes
            ),
            accounted_writer_path_only=(
                account is not None
                and not account.reconciliation_required
                and not account.active_reservations
                and account.ledger_through_sequence == after.through_sequence
            ),
        )
    finally:
        runtime_session.close()


def _benchmark_llm_config() -> LLMConfig:
    slot = ModelSlotConfig(
        model_id="benchmark-model",
        limits=DEFAULT_MODEL_CONTEXT_LIMITS,
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


def _ordered_semantic_content_fingerprint(
    events: tuple[AgentEvent, ...],
) -> str:
    starts = tuple(event for event in events if isinstance(event, TextBlockStartEvent))
    ends = tuple(event for event in events if isinstance(event, TextBlockEndEvent))
    segments = tuple(
        event for event in events if isinstance(event, TextBlockSegmentEvent)
    )
    if len(starts) != 1 or len(ends) != 1 or not segments:
        raise RuntimeError("benchmark requires one completed text block")
    block_id = starts[0].block_id
    if ends[0].block_id != block_id or any(
        segment.block_id != block_id for segment in segments
    ):
        raise RuntimeError("benchmark text block identity drifted")
    return _semantic_content_fingerprint(
        block_id=block_id,
        content="".join(segment.text for segment in segments),
    )


def _semantic_content_fingerprint(*, block_id: str, content: str) -> str:
    return sha256_fingerprint(
        "durable-runtime-benchmark-semantic-content:v2",
        {"kind": "text", "block_id": block_id, "content": content},
    )


def _terminal_projection_text(result: CommittedModelCallResult) -> str:
    if len(result.text_blocks) != 1:
        raise RuntimeError("benchmark requires one terminal text block")
    block = result.text_blocks[0]
    if (
        block.block_id != "benchmark-text-block"
        or block.completion_status != "completed"
        or result.combined_text != block.text
    ):
        raise RuntimeError("benchmark terminal text projection drifted")
    return result.combined_text


def _terminal_projection_semantic_fingerprint(
    events: tuple[AgentEvent, ...],
) -> str:
    ends = tuple(event for event in events if isinstance(event, ModelCallEndEvent))
    if len(ends) != 1:
        raise RuntimeError("benchmark requires exactly one ModelCallEndEvent")
    return (
        ends[0]
        .terminal_projection.projection_reference.semantic_join.semantic_fingerprint
    )


def _physical_settlement_observation(
    events: tuple[AgentEvent, ...],
) -> PhysicalSettlementObservation:
    reservations = tuple(
        event
        for event in events
        if isinstance(event, PhysicalOperationReservationCreatedEvent)
        and event.reservation.owner_kind.value == "model_call"
    )
    settlements = tuple(
        event
        for event in events
        if isinstance(event, PhysicalOperationReservationSettledEvent)
        and event.settlement.owner_kind.value == "model_call"
    )
    rollout_settlements = tuple(
        event
        for event in events
        if isinstance(event, RolloutBudgetReservationSettledEvent)
    )
    if len(reservations) != 1 or len(settlements) != 1:
        raise RuntimeError("benchmark model physical reservation did not settle once")
    if len(rollout_settlements) != 1:
        raise RuntimeError("benchmark rollout reservation did not settle once")
    reservation = reservations[0].reservation
    settlement = settlements[0].settlement
    rollout = rollout_settlements[0]
    released_events = (
        settlement.released_on_suspension_events_lifetime
        + settlement.released_on_settlement_events
    )
    released_payload_bytes = (
        settlement.released_on_suspension_payload_bytes_lifetime
        + settlement.released_on_settlement_payload_bytes
    )
    valid = (
        settlement.reservation_id == reservation.reservation_id
        and settlement.reservation_fingerprint
        == reservation.reservation_fingerprint
        and settlement.terminal_outcome == "completed"
        and settlement.total_charged_events + released_events
        == reservation.reserved_events
        and settlement.total_charged_payload_bytes + released_payload_bytes
        == reservation.reserved_payload_bytes
        and rollout.usage_status == "reserved_missing_usage"
    )
    return PhysicalSettlementObservation(
        valid=valid,
        charged_candidate_events=settlement.charged_candidate_events,
        charged_candidate_payload_bytes=(
            settlement.charged_candidate_payload_bytes
        ),
        charged_wrapper_bytes=settlement.charged_wrapper_bytes,
        charged_bookkeeping_events=settlement.charged_bookkeeping_events,
        charged_bookkeeping_bytes=settlement.charged_bookkeeping_bytes,
        total_charged_events=settlement.total_charged_events,
        total_charged_payload_bytes=settlement.total_charged_payload_bytes,
        released_events=released_events,
        released_payload_bytes=released_payload_bytes,
    )


def _fixed_width_ascii(index: int, width: int) -> str:
    raw = f"{index:0{width}x}"
    if len(raw) != width:
        raise ValueError("benchmark counter exceeded its fixed-width contract")
    return raw


def _hex_identity(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:32]


def _current_wal_lsn(dsn: str) -> str:
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("select pg_current_wal_lsn()::text")
            row = cursor.fetchone()
    if row is None:
        raise RuntimeError("PostgreSQL did not report current WAL LSN")
    return str(row[0])


def _wal_bytes(dsn: str, before: str, after: str) -> int:
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "select pg_wal_lsn_diff(%s::pg_lsn, %s::pg_lsn)::bigint",
                (after, before),
            )
            row = cursor.fetchone()
    if row is None:
        raise RuntimeError("PostgreSQL did not report WAL difference")
    return int(row[0])


def observation_payload(
    observation: ModelSemanticBatchObservation,
) -> bytes:
    """Return a stable debug representation without case-specific event IDs."""

    return canonical_json_bytes(
        {
            "case_id": observation.case_id,
            "semantic_batch_sizes": observation.semantic_batch_sizes,
            "ordered_semantic_content_fingerprint": (
                observation.ordered_semantic_content_fingerprint
            ),
            "terminal_projection_semantic_fingerprint": (
                observation.terminal_projection_semantic_fingerprint
            ),
            "physical_settlement_valid": observation.physical_settlement_valid,
            "accounted_writer_path_only": (
                observation.accounted_writer_path_only
            ),
        }
    )


__all__ = [
    "ModelSemanticBatchObservation",
    "RecordingModelStreamCommitPort",
    "run_model_semantic_batch_sample",
]
