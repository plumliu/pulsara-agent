"""Capability-scoped RuntimeSession adapters for one run activation engine.

The composition root may hold the complete RuntimeSession.  AgentRuntime may
not: it receives these separately scoped adapters and cannot recover the
underlying session from any public API.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence
from time import monotonic

from pulsara_agent.event import AgentEvent
from pulsara_agent.llm.commit import RuntimeSessionModelStreamEventCommitPort
from pulsara_agent.llm.lifecycle import prepare_model_lifecycle_start_bundle
from pulsara_agent.runtime.context_input.live import (
    prepare_live_context_snapshot,
    prepare_live_transcript_projection,
)
from pulsara_agent.runtime.long_horizon.accounting import (
    resolve_run_rollout_binding,
)
from pulsara_agent.runtime.long_horizon.rollup import materialize_observation_rollup
from pulsara_agent.runtime.tool_execution import (
    RuntimeSessionToolExecutionEventCommitPort,
)

if TYPE_CHECKING:
    from pulsara_agent.runtime.session import RuntimeSession


@dataclass(frozen=True, slots=True)
class RunRuntimeIdentity:
    runtime_session_id: str
    workspace_root: Path
    default_event_metadata: Mapping[str, object]
    mcp_installation_id: str
    mcp_installation_owner_runtime_session_id: str
    terminal_owner_host_session_id: str | None
    is_subagent_child: bool


class RuntimeSessionRunLedgerPort:
    """Run event mutation/read capability without a generic companion escape."""

    __slots__ = ("__session",)

    def __init__(self, session: RuntimeSession) -> None:
        self.__session = session

    async def emit(self, event: AgentEvent) -> AgentEvent:
        return await self.__session.emit(event)

    async def emit_many(
        self,
        events: Iterable[AgentEvent],
        *,
        expected_last_sequence: int | None = None,
    ) -> list[AgentEvent]:
        return await self.__session.emit_many(
            events,
            expected_last_sequence=expected_last_sequence,
        )

    async def write_events_with_deadline(
        self,
        events: Sequence[AgentEvent],
        *,
        deadline_monotonic: float,
        expected_last_sequence: int | None = None,
        publication_terminal_maintenance_lease: object | None = None,
    ):
        return await self.__session.write_events_with_deadline(
            events,
            deadline_monotonic=deadline_monotonic,
            expected_last_sequence=expected_last_sequence,
            publication_terminal_maintenance_lease=(
                publication_terminal_maintenance_lease
            ),
        )

    def get_event(self, event_id: str):
        return self.__session.event_log.get_by_id(event_id)

    def iter_events(self, *, run_id: str | None = None):
        return self.__session.event_log.iter(run_id=run_id)

    def read_raw_events_by_id(self, event_ids: Sequence[str], **kwargs):
        return self.__session.event_log.read_raw_events_by_id(event_ids, **kwargs)

    def replay(self, reply_id: str):
        return self.__session.event_log.replay(reply_id)

    def read_raw_range_snapshot(self, **kwargs):
        return self.__session.event_log.read_raw_range_snapshot(**kwargs)

    def next_sequence(self) -> int:
        return self.__session.event_log.next_sequence()

    def final_output_materializer(self):
        from pulsara_agent.runtime.run_execution.final_output import (
            RunFinalOutputMaterializer,
        )

        return RunFinalOutputMaterializer(
            event_log=self.__session.event_log,
            runtime_session_id=self.__session.runtime_session_id,
            io_service=self.__session.context_input_io_service,
            transcript_projection=self.__session.transcript_projection_state_store,
            archive=self.__session.archive,
        )

    def new_write_deadline_monotonic(self) -> float:
        return self.__session.event_write_service.new_deadline_monotonic()

    def resolved_write_outcome(self, error: BaseException):
        return self.__session.resolved_event_write_outcome(error)

    def issue_publication_terminal_maintenance_lease(self, **kwargs):
        return self.__session.issue_publication_terminal_maintenance_lease(**kwargs)

    def latch_event_commit_outcome_unknown(self) -> None:
        self.__session.latch_event_commit_outcome_unknown()

    def latch_context_input_reconciliation_required(self) -> None:
        self.__session.latch_context_input_reconciliation_required()

    @property
    def reconciliation_required(self) -> bool:
        return self.__session.reconciliation_required

    @property
    def publication_reconciliation_required(self) -> bool:
        return self.__session.publication_reconciliation_required


class RuntimeSessionRunContextPort:
    """Context projection, manifest and cache capability."""

    __slots__ = ("__session",)

    def __init__(self, session: RuntimeSession) -> None:
        self.__session = session

    async def prepare_live_transcript_projection(self, **kwargs):
        return await prepare_live_transcript_projection(
            runtime_session=self.__session,
            **kwargs,
        )

    async def prepare_live_context_snapshot(self, **kwargs):
        return await prepare_live_context_snapshot(
            runtime_session=self.__session,
            **kwargs,
        )

    async def materialize_observation_rollup(self, **kwargs):
        return await materialize_observation_rollup(
            runtime_session=self.__session,
            **kwargs,
        )

    async def prepare_run_transcript_seed(self, *, run_id: str):
        from pulsara_agent.runtime.authority_materialization import (
            persist_prepared_run_transcript_seed,
            prepare_authority_artifact_write_reservation,
            prepare_run_transcript_seed,
        )
        from pulsara_agent.runtime.authority_materialization.transcript_reducer import (
            TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT,
        )

        projection_store = self.__session.transcript_projection_state_store
        projection_snapshot = projection_store.snapshot()
        if not projection_snapshot.checkpointable:
            raise RuntimeError(
                "RunStart transcript seed requires a stable projection safe point"
            )
        contracts = self.__session.authority_materialization_contracts
        prepared_seed = prepare_run_transcript_seed(
            runtime_session_id=self.__session.runtime_session_id,
            stable_state=projection_snapshot.stable_semantic_state,
            stable_entries=projection_store.stable_entries(),
            ledger_through_sequence=projection_snapshot.ledger_through_sequence,
            ledger_continuity_accumulator=(
                projection_snapshot.ledger_continuity_accumulator
            ),
            reducer_id="pulsara.transcript-projection",
            reducer_version="1",
            reducer_contract_fingerprint=(
                TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
            ),
            transcript_semantic_domain_contract_fingerprint=(
                contracts.event_domain.contract.registry_contract_fingerprint
            ),
            contracts=self.__session.transcript_projection_materialization_contracts,
        )
        seed_deadline = monotonic() + (
            contracts.limits.checkpoint_operation_timeout_seconds
        )
        reservation = prepare_authority_artifact_write_reservation(
            operation_id=f"run-seed:{run_id}",
            owner_kind="run_seed_materialization",
            artifacts=prepared_seed.artifacts,
            limits=contracts.limits,
            absolute_deadline_monotonic=seed_deadline,
        )
        await self.__session.context_input_io_service.execute(
            operation_name="run-transcript-seed-materialization",
            operation=lambda: persist_prepared_run_transcript_seed(
                prepared_seed,
                write_reservation=reservation,
                limits=contracts.limits,
                archive=self.__session.archive,
                runtime_session_id=self.__session.runtime_session_id,
                deadline_monotonic=seed_deadline,
            ),
            deadline_monotonic=seed_deadline,
        )
        self.__session.transcript_projection_checkpoint_service.prepare_run_seed_artifacts(
            run_id=run_id,
            artifact_ids=frozenset(
                item.artifact_id for item in prepared_seed.artifacts
            ),
        )
        return prepared_seed

    def discard_prepared_run_seed(self, run_id: str) -> None:
        self.__session.transcript_projection_checkpoint_service.discard_prepared_run_seed(
            run_id
        )

    def adopt_committed_run_seed(self, run_start) -> None:
        self.__session.transcript_projection_checkpoint_service.adopt_committed_run_seed(
            run_start
        )

    @property
    def subagent_graph_reducer_contract(self):
        return self.__session.subagent_graph_checkpoint_service.reducer_binding.contract

    @property
    def tool_result_render_cache(self):
        return self.__session.tool_result_render_cache

    @property
    def context_candidate_lifecycle_cache(self):
        return self.__session.context_candidate_lifecycle_cache

    @property
    def prepared_observation_rollup_cache(self):
        return self.__session.prepared_observation_rollup_cache

    @property
    def context_input_manifest_service(self):
        return self.__session.context_input_manifest_service

    @property
    def transcript_projection_checkpoint_service(self):
        return self.__session.transcript_projection_checkpoint_service

    def record_cache_diagnostic(self, **kwargs) -> None:
        self.__session.record_context_input_cache_diagnostic(**kwargs)

    def window_compaction_service(self, *, llm_runtime):
        from pulsara_agent.runtime.long_horizon.window_compaction_service import (
            ContextWindowCompactionService,
        )

        service = self.__session.window_compaction_service
        if service is None:
            service = ContextWindowCompactionService(
                runtime_session=self.__session,
                llm_runtime=llm_runtime,
            )
            self.__session.window_compaction_service = service
        if not isinstance(service, ContextWindowCompactionService):
            raise TypeError(
                "RuntimeSession carries an incompatible window compaction service"
            )
        return service


class RuntimeSessionRunModelPort:
    """Model lifecycle and active-run safe-point capability."""

    __slots__ = ("__session",)

    def __init__(self, session: RuntimeSession) -> None:
        self.__session = session

    @property
    def provider_input_generation_coordinator(self):
        return self.__session.provider_input_generation_coordinator

    @property
    def model_stream_execution_registry(self):
        return self.__session.model_stream_execution_registry

    @property
    def terminal_sessions(self):
        return self.__session.terminal_sessions

    async def borrow_active_run_monitor_safe_point(
        self,
        *,
        run_id: str,
        next_model_call_index: int,
    ):
        return await self.__session.borrow_active_run_monitor_safe_point(
            run_id=run_id,
            next_model_call_index=next_model_call_index,
        )

    def release_active_run_monitor_safe_point(self, lease: object) -> None:
        self.__session.release_active_run_monitor_safe_point(lease)

    def prepare_lifecycle_start_bundle(self, **kwargs):
        return prepare_model_lifecycle_start_bundle(
            runtime_session=self.__session,
            **kwargs,
        )

    def event_commit_port(self) -> RuntimeSessionModelStreamEventCommitPort:
        return RuntimeSessionModelStreamEventCommitPort(runtime_session=self.__session)

    async def resolve_completed_control_call(self, owner, **kwargs):
        return await owner.resolve_completed_call(
            runtime_session=self.__session,
            **kwargs,
        )

    async def request_cancel_run(self, run_id: str, *, reason: str) -> int:
        return await self.__session.model_stream_execution_registry.request_cancel_run(
            run_id,
            reason=reason,
        )


class RuntimeSessionRunToolPort:
    """Tool batch, terminal projection, MCP and notification capability."""

    __slots__ = ("__session", "_subagent_runtime")

    def __init__(self, session: RuntimeSession) -> None:
        self.__session = session
        self._subagent_runtime = None

    def event_commit_port(self) -> RuntimeSessionToolExecutionEventCommitPort:
        return RuntimeSessionToolExecutionEventCommitPort(
            runtime_session=self.__session
        )

    def tool_result_boundary_events(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        start_event_id: str | None = None,
    ) -> list[AgentEvent]:
        from pulsara_agent.event import ToolResultEndEvent, ToolResultStartEvent
        from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY

        ids = (
            start_event_id or f"tool_result_start:{run_id}:{tool_call_id}",
            f"tool_result_end:{run_id}:{tool_call_id}",
        )
        decoded = [
            raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
            for raw in self.__session.event_log.read_raw_events_by_id(ids)
        ]
        if any(
            event.run_id != run_id
            or getattr(event, "tool_call_id", None) != tool_call_id
            or not isinstance(event, (ToolResultStartEvent, ToolResultEndEvent))
            for event in decoded
        ):
            raise RuntimeError("tool-result boundary exact reference identity mismatch")
        return decoded

    def completed_tool_result_events(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        start_event_id: str | None = None,
    ) -> list[AgentEvent]:
        from pulsara_agent.event import (
            ToolResultDataDeltaEvent,
            ToolResultEndEvent,
            ToolResultStartEvent,
            ToolResultTextDeltaEvent,
        )
        from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY

        boundaries = self.tool_result_boundary_events(
            run_id=run_id,
            tool_call_id=tool_call_id,
            start_event_id=start_event_id,
        )
        starts = tuple(
            event for event in boundaries if isinstance(event, ToolResultStartEvent)
        )
        ends = tuple(
            event for event in boundaries if isinstance(event, ToolResultEndEvent)
        )
        if not ends:
            return []
        if len(starts) != 1 or len(ends) != 1:
            raise RuntimeError("completed tool result lacks unique boundaries")
        start_sequence = starts[0].sequence
        end_sequence = ends[0].sequence
        if (
            start_sequence is None
            or end_sequence is None
            or end_sequence < start_sequence
        ):
            raise RuntimeError("completed tool-result sequence range is invalid")
        snapshot = self.__session.event_log.read_raw_range_snapshot(
            minimum_sequence=start_sequence,
            through_sequence=end_sequence,
            max_events=4_096,
            max_payload_bytes=16 * 1024 * 1024,
        )
        return [
            decoded
            for raw in snapshot.events
            if (decoded := raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)).run_id
            == run_id
            and getattr(decoded, "tool_call_id", None) == tool_call_id
            and isinstance(
                decoded,
                (
                    ToolResultStartEvent,
                    ToolResultTextDeltaEvent,
                    ToolResultDataDeltaEvent,
                    ToolResultEndEvent,
                ),
            )
        ]

    def build_composition_input(self, *, memory_hooks, subagent_runtime):
        from pulsara_agent.runtime.tool_composition import (
            build_runtime_tool_composition_input,
        )

        return build_runtime_tool_composition_input(
            self.__session,
            memory_proposal_sink=getattr(memory_hooks, "memory_proposal_sink", None),
            memory_recall_service=getattr(memory_hooks, "recall", None),
            memory_query=getattr(memory_hooks, "memory_query", None),
            graph_id=getattr(memory_hooks, "graph_id", None),
            memory_read_scopes=getattr(memory_hooks, "read_scopes", None),
            subagent_runtime=subagent_runtime,
        )

    def ensure_subagent_runtime(self, *, enabled: bool):
        if not enabled:
            return None
        from pulsara_agent.event_log import InMemoryEventLog, PostgresEventLog
        from pulsara_agent.runtime.subagent import (
            InMemoryEventLogLocator,
            PostgresEventLogLocator,
            SubagentRuntime,
        )

        existing = self._subagent_runtime
        if isinstance(existing, SubagentRuntime):
            return existing
        parent_log = self.__session.event_log
        if isinstance(parent_log, InMemoryEventLog):
            locator = InMemoryEventLogLocator()

            def factory(runtime_session_id: str):
                event_log = InMemoryEventLog(runtime_session_id=runtime_session_id)
                locator.register(runtime_session_id, event_log)
                return event_log

        elif isinstance(parent_log, PostgresEventLog):
            locator = PostgresEventLogLocator(
                connection_provider=parent_log.connection_provider,
                workspace_root=self.__session.workspace_root,
            )
            factory = locator.event_log_for_runtime_session
        else:
            raise TypeError(
                "SubagentRuntime requires a supported EventLog backend "
                f"(got {type(parent_log).__name__})"
            )
        runtime = SubagentRuntime(
            parent_runtime_session=self.__session,
            child_event_log_factory=factory,
            event_log_locator=locator,
        )
        self._subagent_runtime = runtime
        return runtime

    def make_thread_recorder(self):
        return self.__session.make_thread_recorder()

    async def ensure_physical_operation_headroom(self, operation_kind) -> None:
        await self.__session.ensure_physical_operation_headroom(operation_kind)

    @property
    def publisher(self):
        return self.__session.publisher

    @property
    def physical_dispatch_capacity(self):
        return self.__session.physical_dispatch_capacity

    @property
    def tool_execution_terminal_registry(self):
        return self.__session.tool_execution_terminal_registry

    @property
    def tool_terminal_projection_service(self):
        return self.__session.tool_terminal_projection_service

    @property
    def mcp_tool_execution_port(self):
        return self.__session.mcp_tool_execution_port

    @property
    def terminal_monitor_coordinator(self):
        return self.__session.terminal_monitor_coordinator

    @property
    def terminal_notification_store(self):
        return self.__session.terminal_notification_store

    @property
    def terminal_notification_account_coordinator(self):
        return self.__session.terminal_notification_account_coordinator


class RuntimeSessionRunLongHorizonPort:
    """Long-horizon reducer and artifact authority."""

    __slots__ = ("__session",)

    def __init__(self, session: RuntimeSession) -> None:
        self.__session = session

    @property
    def store(self):
        return self.__session.long_horizon_state_store

    @property
    def archive(self):
        return self.__session.archive

    def resolve_rollout_binding(self, *, run_id: str):
        return resolve_run_rollout_binding(self.__session, run_id=run_id)


class RuntimeSessionRunAuditPort:
    """Mandatory runtime audit owner capability."""

    __slots__ = ("__session",)

    def __init__(self, session: RuntimeSession) -> None:
        self.__session = session

    @property
    def mandatory_owner(self):
        return self.__session.mandatory_runtime_audit_owner


def build_run_runtime_identity(session: RuntimeSession) -> RunRuntimeIdentity:
    metadata = MappingProxyType(dict(session.default_event_metadata))
    return RunRuntimeIdentity(
        runtime_session_id=session.runtime_session_id,
        workspace_root=session.workspace_root,
        default_event_metadata=metadata,
        mcp_installation_id=session.mcp_installation_id,
        mcp_installation_owner_runtime_session_id=(
            session.mcp_installation_owner_runtime_session_id
        ),
        terminal_owner_host_session_id=session.terminal_owner_host_session_id,
        is_subagent_child=isinstance(metadata.get("subagent"), dict),
    )


def build_agent_runtime_session_capabilities(session: RuntimeSession) -> dict[str, Any]:
    """Return explicit constructor kwargs; AgentRuntime never stores this bundle."""

    return {
        "run_identity": build_run_runtime_identity(session),
        "run_ledger_port": RuntimeSessionRunLedgerPort(session),
        "run_context_port": RuntimeSessionRunContextPort(session),
        "run_model_port": RuntimeSessionRunModelPort(session),
        "run_tool_port": RuntimeSessionRunToolPort(session),
        "run_long_horizon_port": RuntimeSessionRunLongHorizonPort(session),
        "run_audit_port": RuntimeSessionRunAuditPort(session),
    }


__all__ = [
    "RunRuntimeIdentity",
    "RuntimeSessionRunAuditPort",
    "RuntimeSessionRunContextPort",
    "RuntimeSessionRunLedgerPort",
    "RuntimeSessionRunLongHorizonPort",
    "RuntimeSessionRunModelPort",
    "RuntimeSessionRunToolPort",
    "build_agent_runtime_session_capabilities",
    "build_run_runtime_identity",
]
