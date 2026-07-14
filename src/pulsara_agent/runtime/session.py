"""Runtime session ownership for one active Pulsara backend run."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import Any, Iterable, Literal
from uuid import uuid4

from pulsara_agent.event import (
    AgentEvent,
    CapabilityExposureResolvedEvent,
    ContextCompactionCompletedEvent,
    ContextCompactionFailedEvent,
    ContextCompactionStartedEvent,
    ContextWindowClosedEvent,
    ContextWindowCompactionCompletedEvent,
    ContextWindowCompactionFailedEvent,
    ContextWindowCompactionStartedEvent,
    ContextWindowOpenedEvent,
    McpCapabilitySnapshotInstalledEvent,
    RolloutBudgetReservationSettledEvent,
    EventType,
    RunEndEvent,
    RunInteractionResumeBoundaryEvent,
    RunStartEvent,
)
from pulsara_agent.event_log import (
    EventBatchConfirmation,
    EventIdConflict,
    EventLog,
    EventLogWriteConflict,
    DEFAULT_EVENT_SCHEMA_REGISTRY,
)
from pulsara_agent.memory.candidates.proposal_sink import MemoryProposalSink
from pulsara_agent.llm.execution import ModelStreamExecutionRegistry
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.primitives.context import ContextStaticInstructionFact
from pulsara_agent.runtime.hooks import RuntimeHookManager
from pulsara_agent.runtime.context_input.candidate import InMemoryContextLifecycleCache
from pulsara_agent.runtime.context_input.event_slice import (
    InMemoryContextAuthoritySliceCache,
)
from pulsara_agent.runtime.context_input.manifest import (
    ContextInputManifestWriteService,
)
from pulsara_agent.runtime.context_input.io_service import ContextInputIoService
from pulsara_agent.runtime.event_write_service import (
    PendingRuntimeEventWriteError,
    RuntimeEventWriteCancelled,
    RuntimeEventWriteService,
)
from pulsara_agent.runtime.context_input.render import InMemoryToolResultRenderCache
from pulsara_agent.runtime.mcp.types import McpPendingInstallationAudit
from pulsara_agent.runtime.permission import PermissionState
from pulsara_agent.runtime.publisher import (
    PublisherEnqueueResult,
    RuntimeEventPublisher,
    RuntimePublishedEvent,
)
from pulsara_agent.runtime.state import LoopState
from pulsara_agent.runtime.terminal import (
    BorrowedWorkspaceTerminalRuntime,
    OwnedTerminalRuntime,
    TerminalOwnerContext,
    TerminalRuntimeBinding,
    TerminalSessionManager,
)
from pulsara_agent.runtime.tool_artifacts import (
    ToolResultArtifactIndex,
    ToolResultArtifactService,
)
from pulsara_agent.runtime.tool_execution import ToolExecutionTerminalRegistry
from pulsara_agent.tools.base import AsyncTool, Tool


@dataclass(frozen=True, slots=True)
class RuntimeThreadRecorder:
    runtime_session: "RuntimeSession"
    state: LoopState | None = None

    def __call__(self, event: AgentEvent) -> AgentEvent:
        return self.runtime_session.emit_from_thread(event, state=self.state)


@dataclass(frozen=True, slots=True)
class CommittedReducerError:
    reducer_id: str
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class EventPublicationError:
    event_id: str
    sequence: int
    subscriber_id: str | None
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class EventWriteResult:
    committed_events: tuple[AgentEvent, ...]
    commit_status: Literal["committed"]
    reducer_high_waters: Mapping[str, int]
    reconciliation_required: bool
    reducer_errors: tuple[CommittedReducerError, ...]
    publication_status: Literal["completed", "enqueued", "unavailable"]
    publisher_enqueued_through_sequence: int | None
    publication_errors: tuple[EventPublicationError, ...] = ()

    def require_reduced(self, reducer_id: str) -> tuple[AgentEvent, ...]:
        last_sequence = max(
            (event.sequence or 0 for event in self.committed_events),
            default=0,
        )
        reducer_sequence = self.reducer_high_waters.get(reducer_id)
        reducer_failed = any(
            error.reducer_id == reducer_id for error in self.reducer_errors
        )
        if (
            reducer_sequence is None
            or reducer_sequence < last_sequence
            or reducer_failed
        ):
            raise EventReconciliationRequired(
                f"Committed reducer {reducer_id!r} did not apply through sequence {last_sequence}"
            )
        return self.committed_events


@dataclass(frozen=True, slots=True)
class RuntimeEventSnapshot:
    events: tuple[AgentEvent, ...]
    through_sequence: int


@dataclass(frozen=True, slots=True)
class EventBatchCommitOutcome:
    """Terminal durable result owned by one event-writer attempt."""

    status: Literal["full", "none", "unknown"]
    deadline_monotonic: float
    result: EventWriteResult | None = None

    def __post_init__(self) -> None:
        if self.status == "full" and self.result is None:
            raise ValueError("FULL event commit outcome requires its write result")
        if self.status != "full" and self.result is not None:
            raise ValueError("non-FULL event commit outcome cannot carry a result")

    @property
    def committed_events(self) -> tuple[AgentEvent, ...]:
        return self.result.committed_events if self.result is not None else ()


class EventCommitError(RuntimeError):
    """Event batch was not durably committed."""

    def __init__(
        self,
        message: str,
        *,
        commit_outcome: Literal["none", "unknown"] = "none",
        deadline_monotonic: float | None = None,
    ) -> None:
        self.commit_outcome = commit_outcome
        self.deadline_monotonic = deadline_monotonic
        super().__init__(message)


class EventWriteCancelled(asyncio.CancelledError):
    """Caller cancellation after the critical writer resolved durable state."""

    def __init__(self, outcome: EventBatchCommitOutcome) -> None:
        self.outcome = outcome
        super().__init__(f"event write cancelled with {outcome.status} commit outcome")


class EventWriteConflict(RuntimeError):
    def __init__(
        self,
        *,
        runtime_session_id: str,
        expected_last_sequence: int,
        actual_last_sequence: int,
        deadline_monotonic: float | None = None,
    ) -> None:
        self.runtime_session_id = runtime_session_id
        self.expected_last_sequence = expected_last_sequence
        self.actual_last_sequence = actual_last_sequence
        self.deadline_monotonic = deadline_monotonic
        super().__init__(
            f"Runtime session {runtime_session_id} write conflict: expected "
            f"{expected_last_sequence}, actual {actual_last_sequence}"
        )


class EventReconciliationRequired(RuntimeError):
    """A committed reducer is inconsistent and mutation must fail closed."""


class EventPublicationAfterCommitError(RuntimeError):
    def __init__(self, result: EventWriteResult) -> None:
        self.result = result
        super().__init__("Event batch committed but one or more observers failed")


def event_batch_commit_outcome_from_error(
    error: BaseException,
) -> EventBatchCommitOutcome | None:
    """Return an outcome already resolved by the writer without new ledger I/O."""

    if isinstance(error, EventWriteCancelled):
        return error.outcome
    if isinstance(error, EventPublicationAfterCommitError):
        return EventBatchCommitOutcome(
            status="full",
            deadline_monotonic=monotonic(),
            result=error.result,
        )
    if isinstance(error, EventCommitError):
        return EventBatchCommitOutcome(
            status=error.commit_outcome,
            deadline_monotonic=(
                error.deadline_monotonic
                if error.deadline_monotonic is not None
                else monotonic()
            ),
        )
    if isinstance(error, EventWriteConflict) and error.deadline_monotonic is not None:
        return EventBatchCommitOutcome(
            status="none",
            deadline_monotonic=error.deadline_monotonic,
        )
    return None


class PublisherSequenceGapError(RuntimeError):
    """Committed publisher catch-up interval is unavailable or non-contiguous."""


@dataclass(slots=True)
class _CommittedReducerRegistration:
    reducer_id: str
    through_sequence: int
    apply_committed: Callable[[tuple[AgentEvent, ...]], None]
    rebuild_committed: Callable[[tuple[AgentEvent, ...]], None] | None = None
    reconciliation_required: bool = False
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class _WriteAttempt:
    result: EventWriteResult
    delivery_futures: tuple[Future[None], ...]
    published_events: tuple[AgentEvent, ...]


@dataclass(slots=True)
class SessionWriteCoordinator:
    """One thread-safe serialization boundary for async and thread writers."""

    lock: RLock = field(default_factory=RLock)


@dataclass(slots=True)
class RuntimeSession:
    workspace_root: Path
    event_log: EventLog
    archive: ArtifactStore
    tool_result_artifacts: ToolResultArtifactIndex
    runtime_session_id: str = field(default_factory=lambda: f"runtime:{uuid4().hex}")
    hook_manager: RuntimeHookManager = field(default_factory=RuntimeHookManager)
    memory_proposal_sink: MemoryProposalSink = field(default_factory=MemoryProposalSink)
    terminal_binding: TerminalRuntimeBinding | None = None
    extra_tool_bindings: tuple[Tool | AsyncTool, ...] = ()
    subagent_runtime: Any | None = None
    window_compaction_service: Any | None = None
    mcp_supervisor: Any | None = None
    context_event_log_locator: Any | None = None
    rollout_account_owner_state_store: Any | None = None
    default_event_metadata: dict[str, Any] = field(default_factory=dict)
    publisher: RuntimeEventPublisher = field(init=False)
    write_coordinator: SessionWriteCoordinator = field(
        default_factory=SessionWriteCoordinator,
        init=False,
    )
    terminal_sessions: TerminalSessionManager = field(init=False)
    artifact_service: ToolResultArtifactService = field(init=False)
    context_input_manifest_service: ContextInputManifestWriteService = field(
        init=False,
        repr=False,
    )
    context_input_io_service: ContextInputIoService = field(
        init=False,
        repr=False,
    )
    event_write_service: RuntimeEventWriteService = field(
        init=False,
        repr=False,
    )
    context_static_instruction_cache: dict[
        tuple[str, str, str], ContextStaticInstructionFact
    ] = field(default_factory=dict, init=False, repr=False)
    context_candidate_lifecycle_cache: InMemoryContextLifecycleCache = field(
        init=False,
        repr=False,
    )
    context_authority_slice_cache: InMemoryContextAuthoritySliceCache = field(
        init=False,
        repr=False,
    )
    tool_result_render_cache: InMemoryToolResultRenderCache = field(
        init=False,
        repr=False,
    )
    subagent_graph_checkpoint_service: Any = field(
        init=False,
        repr=False,
    )
    long_horizon_state_store: Any = field(
        init=False,
        repr=False,
    )
    observation_rollup_content_cache: Any = field(
        init=False,
        repr=False,
    )
    prepared_observation_rollup_cache: Any = field(
        init=False,
        repr=False,
    )
    model_stream_execution_registry: ModelStreamExecutionRegistry = field(
        init=False,
        repr=False,
    )
    tool_execution_terminal_registry: ToolExecutionTerminalRegistry = field(
        init=False,
        repr=False,
    )
    _owns_terminal_manager: bool = field(default=False, init=False, repr=False)
    _terminal_owner: TerminalOwnerContext | None = field(
        default=None, init=False, repr=False
    )
    _committed_reducers: dict[str, _CommittedReducerRegistration] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _reconciliation_required: bool = field(default=False, init=False, repr=False)
    _ledger_reconciliation_required: bool = field(default=False, init=False, repr=False)
    _context_input_reconciliation_required: bool = field(
        default=False,
        init=False,
        repr=False,
    )
    mcp_installation_id: str = field(default="mcp_installation:empty", init=False)
    mcp_installation_owner_runtime_session_id: str = field(init=False)
    _pending_mcp_installation_audits: list[McpPendingInstallationAudit] = field(
        default_factory=list,
        init=False,
        repr=False,
    )
    _context_input_cache_diagnostics: list[dict[str, str]] = field(
        default_factory=list,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        self.workspace_root = self.workspace_root.expanduser().resolve()
        bind_event_log_owner = getattr(self.event_log, "bind_runtime_session_id", None)
        if bind_event_log_owner is not None:
            bind_event_log_owner(self.runtime_session_id)
        self.mcp_installation_owner_runtime_session_id = self.runtime_session_id
        self.artifact_service = ToolResultArtifactService(
            archive=self.archive,
            index=self.tool_result_artifacts,
            runtime_session_id=self.runtime_session_id,
        )
        self.context_input_manifest_service = ContextInputManifestWriteService(
            archive=self.archive,
            on_consistency_failure=self.latch_context_input_reconciliation_required,
        )
        self.context_input_io_service = ContextInputIoService()
        self.event_write_service = RuntimeEventWriteService()
        self.context_candidate_lifecycle_cache = InMemoryContextLifecycleCache()
        self.context_authority_slice_cache = InMemoryContextAuthoritySliceCache()
        self.tool_result_render_cache = InMemoryToolResultRenderCache()
        self.model_stream_execution_registry = ModelStreamExecutionRegistry()
        self.tool_execution_terminal_registry = ToolExecutionTerminalRegistry(self)
        from pulsara_agent.primitives.long_horizon import (
            default_subagent_graph_checkpoint_policy,
        )
        from pulsara_agent.runtime.long_horizon.checkpoint_store import (
            SubagentGraphCheckpointService,
        )
        from pulsara_agent.runtime.long_horizon.reducer_contract import (
            build_default_subagent_graph_reducer_binding,
        )

        self.subagent_graph_checkpoint_service = SubagentGraphCheckpointService(
            runtime_session=self,
            reducer_binding=build_default_subagent_graph_reducer_binding(),
            policy=default_subagent_graph_checkpoint_policy(),
        )
        self.publisher = RuntimeEventPublisher(
            runtime_session_id=self.runtime_session_id,
            next_sequence_to_publish=_next_publish_sequence(self.event_log),
        )
        self.publisher.subscribe(self.hook_manager)
        from pulsara_agent.runtime.long_horizon.store import LongHorizonStateStore
        from pulsara_agent.runtime.long_horizon.rollup import (
            InMemoryObservationRollupContentCache,
            InMemoryPreparedObservationRollupCache,
        )

        self.observation_rollup_content_cache = (
            InMemoryObservationRollupContentCache()
        )
        self.prepared_observation_rollup_cache = (
            InMemoryPreparedObservationRollupCache()
        )

        long_horizon_types = (
            EventType.RUN_START.value,
            EventType.RUN_END.value,
            EventType.CONTEXT_WINDOW_OPENED.value,
            EventType.CONTEXT_WINDOW_CLOSED.value,
            EventType.CONTEXT_PROJECTION_REWRITE_PAGE.value,
            EventType.ROLLOUT_BUDGET_ACCOUNT_OPENED.value,
            EventType.ROLLOUT_BUDGET_ACCOUNT_CLOSED.value,
            EventType.ROLLOUT_BUDGET_RESERVATION_CREATED.value,
            EventType.ROLLOUT_BUDGET_RESERVATION_SETTLED.value,
            EventType.ROLLOUT_PHASE_TRANSITIONED.value,
            EventType.CONTEXT_WINDOW_COMPACTION_STARTED.value,
            EventType.CONTEXT_WINDOW_COMPACTION_COMPLETED.value,
            EventType.CONTEXT_WINDOW_COMPACTION_FAILED.value,
        )
        bootstrap_deadline = monotonic() + 30.0
        long_horizon_bootstrap = self.event_log.read_raw_events_by_types(
            long_horizon_types,
            active_runs_only=True,
            deadline_monotonic=bootstrap_deadline,
        )
        self.long_horizon_state_store = LongHorizonStateStore.from_sparse_bootstrap(
            tuple(
                event.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
                for event in long_horizon_bootstrap.events
            ),
            through_sequence=long_horizon_bootstrap.through_sequence,
        )
        self.register_committed_reducer(
            reducer_id=(
                f"long_horizon:{self.runtime_session_id}"
            ),
            through_sequence=self.long_horizon_state_store.through_sequence,
            apply_committed=self.long_horizon_state_store.apply_committed,
            rebuild_committed=self.long_horizon_state_store.rebuild,
        )
        self._bind_terminal(self.terminal_binding)

    def set_mcp_installation_contract(
        self,
        *,
        installation_id: str,
        owner_runtime_session_id: str | None = None,
        pending_audit: McpPendingInstallationAudit | None = None,
    ) -> None:
        self.mcp_installation_id = installation_id
        self.mcp_installation_owner_runtime_session_id = (
            owner_runtime_session_id or self.runtime_session_id
        )
        if pending_audit is not None:
            self._queue_mcp_installation_audit(pending_audit)

    def pending_mcp_installation_audit_events(self, context) -> tuple[AgentEvent, ...]:
        if not self._pending_mcp_installation_audits:
            return ()
        from pulsara_agent.event import McpCapabilitySnapshotInstalledEvent

        return tuple(
            McpCapabilitySnapshotInstalledEvent(
                **context.event_fields(),
                id=audit.event_id,
                installation_id=audit.installation_id,
                previous_installation_id=audit.previous_installation_id,
                config_epoch=audit.config_epoch,
                event_safe_config_set_fingerprint=audit.event_safe_config_set_fingerprint,
                installation_triggers=audit.installation_triggers,
                coalesced_installation_count=audit.coalesced_installation_count,
                coalesced_attempt_summaries=audit.coalesced_attempt_summaries,
                coalesced_attempt_summaries_omitted=audit.coalesced_attempt_summaries_omitted,
                server_snapshots=audit.server_snapshots,
                total_installed_tool_count=audit.total_installed_tool_count,
                added_tool_count=audit.added_tool_count,
                revoked_tool_count=audit.revoked_tool_count,
                changed_tool_names_bounded=audit.changed_tool_names_bounded,
                changed_tool_names_omitted=audit.changed_tool_names_omitted,
                diagnostics=audit.diagnostics,
            )
            for audit in self._pending_mcp_installation_audits
        )

    def acknowledge_mcp_installation_audits(self, event_ids: set[str]) -> None:
        self._pending_mcp_installation_audits = [
            audit
            for audit in self._pending_mcp_installation_audits
            if audit.event_id not in event_ids
        ]

    def acknowledge_committed_mcp_installation_audits(
        self,
        events: Iterable[AgentEvent],
    ) -> None:
        """Drop pending audit ownership using canonical committed events only."""

        from pulsara_agent.event import McpCapabilitySnapshotInstalledEvent

        self.acknowledge_mcp_installation_audits(
            {
                event.id
                for event in events
                if isinstance(event, McpCapabilitySnapshotInstalledEvent)
            }
        )

    def _queue_mcp_installation_audit(self, audit: McpPendingInstallationAudit) -> None:
        if not self._pending_mcp_installation_audits:
            self._pending_mcp_installation_audits.append(audit)
            return
        previous = self._pending_mcp_installation_audits[0]
        summaries = list(previous.coalesced_attempt_summaries)
        summaries.extend(
            snapshot.attempt
            for snapshot in previous.server_snapshots
            if snapshot.changed_in_this_installation
        )
        summaries.extend(audit.coalesced_attempt_summaries)
        bounded = tuple(summaries[:64])
        omitted = (
            previous.coalesced_attempt_summaries_omitted
            + audit.coalesced_attempt_summaries_omitted
            + max(0, len(summaries) - len(bounded))
        )
        triggers = sorted(
            {
                *(audit.installation_triggers),
                *(summary.reconcile_trigger for summary in bounded),
            }
        )
        baseline_names = previous.baseline_tool_names
        current_names = audit.current_tool_names
        changed_names = tuple(
            sorted(baseline_names.symmetric_difference(current_names))
        )
        bounded_changed_names = changed_names[:64]
        rebuilt = replace(
            audit,
            event_id=f"mcp_installation_event:{uuid4().hex}",
            previous_installation_id=previous.previous_installation_id,
            installation_triggers=tuple(triggers),
            coalesced_installation_count=(
                previous.coalesced_installation_count
                + audit.coalesced_installation_count
                + 1
            ),
            coalesced_attempt_summaries=bounded,
            coalesced_attempt_summaries_omitted=omitted,
            added_tool_count=len(current_names.difference(baseline_names)),
            revoked_tool_count=len(baseline_names.difference(current_names)),
            changed_tool_names_bounded=bounded_changed_names,
            changed_tool_names_omitted=max(
                0, len(changed_names) - len(bounded_changed_names)
            ),
            baseline_tool_names=baseline_names,
            current_tool_names=current_names,
        )
        self._pending_mcp_installation_audits = [rebuilt]

    def _bind_terminal(self, binding: TerminalRuntimeBinding | None) -> None:
        # Default is owned-local: a bare RuntimeSession(workspace_root) keeps a
        # private manager it shuts down on close. HostCore injects a borrowed
        # binding whose lease release is the supervisor's job, not ours.
        binding = binding or OwnedTerminalRuntime()
        self.terminal_binding = binding
        if isinstance(binding, BorrowedWorkspaceTerminalRuntime):
            self.terminal_sessions = binding.manager
            self._owns_terminal_manager = False
            self._terminal_owner = binding.owner
        else:
            self.terminal_sessions = binding.manager or TerminalSessionManager(
                self.workspace_root
            )
            self._owns_terminal_manager = True
            self._terminal_owner = None

    @property
    def terminal_owner_host_session_id(self) -> str | None:
        return (
            self._terminal_owner.host_session_id
            if self._terminal_owner is not None
            else None
        )

    @property
    def terminal_owner_conversation_id(self) -> str | None:
        return (
            self._terminal_owner.conversation_id
            if self._terminal_owner is not None
            else None
        )

    def _require_runtime_managed_sequence(self, event: AgentEvent) -> None:
        if event.sequence is not None:
            raise ValueError(
                "RuntimeSession.emit requires sequence=None; canonical sequence is assigned by EventLog"
            )

    def _with_default_metadata(self, event: AgentEvent) -> AgentEvent:
        if not self.default_event_metadata:
            return event
        metadata = _merge_event_metadata(self.default_event_metadata, event.metadata)
        if metadata == event.metadata:
            return event
        return event.model_copy(update={"metadata": metadata})

    def _require_default_metadata_present(self, event: AgentEvent) -> None:
        if not self.default_event_metadata:
            return
        missing = [
            key
            for key, value in self.default_event_metadata.items()
            if key not in event.metadata
            or not _metadata_contains_default(event.metadata[key], value)
        ]
        if missing:
            raise ValueError(
                "Stored event is missing RuntimeSession default metadata values: "
                + ", ".join(sorted(missing))
            )

    @property
    def reconciliation_required(self) -> bool:
        return (
            self._ledger_reconciliation_required
            or self._context_input_reconciliation_required
            or self._reconciliation_required
        )

    @property
    def ledger_reconciliation_required(self) -> bool:
        return self._ledger_reconciliation_required

    def latch_event_commit_outcome_unknown(self) -> None:
        """Fail closed when stable event IDs cannot establish commit existence."""

        with self.write_coordinator.lock:
            self._latch_ledger_reconciliation_required()

    def resolved_event_write_outcome(
        self,
        error: BaseException,
    ) -> EventBatchCommitOutcome:
        """Consume the writer-owned outcome; never perform recovery ledger I/O."""

        outcome = event_batch_commit_outcome_from_error(error)
        if outcome is not None:
            if outcome.status == "unknown":
                self.latch_event_commit_outcome_unknown()
            return outcome
        self.latch_event_commit_outcome_unknown()
        return EventBatchCommitOutcome(
            status="unknown",
            deadline_monotonic=monotonic(),
        )

    def latch_context_input_reconciliation_required(self) -> None:
        """Block mutation after manifest identity/confirmation inconsistency."""

        with self.write_coordinator.lock:
            self._context_input_reconciliation_required = True

    def record_context_input_cache_diagnostic(
        self,
        *,
        cache_kind: str,
        operation: str,
        error: BaseException,
    ) -> None:
        self._context_input_cache_diagnostics.append(
            {
                "cache_kind": cache_kind,
                "operation": operation,
                "error_type": type(error).__name__,
                "message": str(error)[:240],
            }
        )
        del self._context_input_cache_diagnostics[:-32]

    def context_input_cache_diagnostics(self) -> tuple[dict[str, str], ...]:
        return tuple(dict(item) for item in self._context_input_cache_diagnostics)

    def require_mutation_allowed(self) -> None:
        """Fail before any safe-point side effect when the ledger is latched."""

        if self.reconciliation_required:
            raise EventReconciliationRequired(
                "RuntimeSession ledger or committed reducer requires reconciliation"
            )

    def read_event_snapshot_through_current(self) -> RuntimeEventSnapshot:
        """Read one contiguous ledger snapshot under the session write boundary."""

        with self.write_coordinator.lock:
            self.require_mutation_allowed()
            events = tuple(self.event_log.iter())
            through_sequence = self.event_log.next_sequence() - 1
            if events:
                sequences = [_event_sequence(event) for event in events]
                if sequences != list(range(1, through_sequence + 1)):
                    self._latch_ledger_reconciliation_required()
                    raise EventReconciliationRequired(
                        "RuntimeSession event snapshot is not contiguous"
                    )
            elif through_sequence != 0:
                self._latch_ledger_reconciliation_required()
                raise EventReconciliationRequired(
                    "RuntimeSession empty snapshot has non-zero high-water"
                )
            return RuntimeEventSnapshot(
                events=events,
                through_sequence=through_sequence,
            )

    def confirm_event_batch(
        self,
        candidates: Sequence[AgentEvent],
    ) -> EventBatchConfirmation:
        """Confirm a stable candidate batch after cancellation/unknown ack.

        Candidates are normalized with the same RuntimeSession metadata overlay
        used for the original write.  A partial atomic batch is structural ledger
        corruption and permanently latches mutation.
        """

        deadline = self.event_write_service.current_deadline_monotonic()
        prepared = self._prepare_event_batch(candidates)
        return self._confirm_event_batch_owned(prepared, deadline_monotonic=deadline)

    async def confirm_event_batch_async(
        self,
        candidates: Sequence[AgentEvent],
        *,
        deadline_monotonic: float,
    ) -> EventBatchConfirmation:
        """Continue one bounded write attempt through the critical FIFO."""

        prepared = self._prepare_event_batch(candidates)
        return await self.event_write_service.execute(
            lambda: self._confirm_event_batch_owned(
                prepared,
                deadline_monotonic=deadline_monotonic,
            ),
            deadline_monotonic=deadline_monotonic,
        )

    def confirm_event_batch_from_thread(
        self,
        candidates: Sequence[AgentEvent],
        *,
        deadline_monotonic: float | None = None,
    ) -> EventBatchConfirmation:
        """Join the critical FIFO from a service-owned blocking worker."""

        prepared = self._prepare_event_batch(candidates)
        deadline = (
            self.event_write_service.new_deadline_monotonic()
            if deadline_monotonic is None
            else deadline_monotonic
        )
        return self.event_write_service.execute_blocking(
            lambda: self._confirm_event_batch_owned(
                prepared,
                deadline_monotonic=deadline,
            ),
            deadline_monotonic=deadline,
        )

    def _confirm_event_batch_owned(
        self,
        prepared: tuple[AgentEvent, ...],
        *,
        deadline_monotonic: float,
    ) -> EventBatchConfirmation:
        with self.write_coordinator.lock:
            confirmation = self.event_log.confirm_batch(
                prepared,
                deadline_monotonic=deadline_monotonic,
            )
            committed = confirmation.committed_events
            if committed and confirmation.missing_event_ids:
                self._latch_ledger_reconciliation_required()
                raise EventReconciliationRequired(
                    "Only part of an atomic event batch can be confirmed by id"
                )
            if committed:
                sequences = [_event_sequence(event) for event in committed]
                if sequences != list(range(sequences[0], sequences[-1] + 1)):
                    self._latch_ledger_reconciliation_required()
                    raise EventReconciliationRequired(
                        f"Confirmed event batch is not contiguous: {sequences}"
                    )
            return confirmation

    async def confirm_and_handoff_event_batch_async(
        self,
        candidates: Sequence[AgentEvent],
        *,
        state: LoopState | None = None,
        deadline_monotonic: float,
    ) -> EventWriteResult:
        prepared = self._prepare_event_batch(candidates)
        return await self.event_write_service.execute(
            lambda: self._confirm_and_handoff_event_batch_owned(
                prepared,
                state=state,
                deadline_monotonic=deadline_monotonic,
            ),
            deadline_monotonic=deadline_monotonic,
        )

    def confirm_and_handoff_event_batch_from_thread(
        self,
        candidates: Sequence[AgentEvent],
        *,
        state: LoopState | None = None,
        deadline_monotonic: float | None = None,
    ) -> EventWriteResult:
        prepared = self._prepare_event_batch(candidates)
        deadline = (
            self.event_write_service.new_deadline_monotonic()
            if deadline_monotonic is None
            else deadline_monotonic
        )
        return self.event_write_service.execute_blocking(
            lambda: self._confirm_and_handoff_event_batch_owned(
                prepared,
                state=state,
                deadline_monotonic=deadline,
            ),
            deadline_monotonic=deadline,
        )

    def confirm_and_handoff_event_batch(
        self,
        candidates: Sequence[AgentEvent],
        *,
        state: LoopState | None = None,
    ) -> EventWriteResult:
        """Confirm a stable batch and restore its process-local handoff.

        This is the post-commit acknowledgement recovery path for service-owned
        writers. It never creates facts: every candidate must already exist in
        full with the same immutable payload. Reducers and the ordered publisher
        are then caught up under the same session write boundary.
        """

        deadline = self.event_write_service.current_deadline_monotonic()
        prepared = self._prepare_event_batch(candidates)
        return self._confirm_and_handoff_event_batch_owned(
            prepared,
            state=state,
            deadline_monotonic=deadline,
        )

    def _confirm_and_handoff_event_batch_owned(
        self,
        prepared: tuple[AgentEvent, ...],
        *,
        state: LoopState | None,
        deadline_monotonic: float,
    ) -> EventWriteResult:
        with self.write_coordinator.lock:
            try:
                committed, high_water = self._confirm_committed_batch(
                    prepared,
                    deadline_monotonic=deadline_monotonic,
                )
            except (EventIdConflict, EventReconciliationRequired):
                raise
            except BaseException:
                self._latch_ledger_reconciliation_required()
                raise
            if committed is None:
                raise EventCommitError(
                    "Stable event batch is absent from the durable ledger",
                    commit_outcome="none",
                    deadline_monotonic=deadline_monotonic,
                )
            return self._reconcile_confirmed_attempt(
                committed,
                catch_up_through_sequence=high_water,
                state=state,
                await_delivery=False,
            ).result

    def confirm_and_reduce_event_batch(
        self,
        candidates: Sequence[AgentEvent],
        *,
        state: LoopState | None = None,
    ) -> EventWriteResult:
        """Confirm and fold a stable batch without touching the publisher.

        This narrow phase is used by control-plane linearization. Publication
        must happen after its process-local control lock is released.
        """

        prepared = self._prepare_event_batch(candidates)
        deadline = self.event_write_service.current_deadline_monotonic()
        with self.write_coordinator.lock:
            try:
                committed, high_water = self._confirm_committed_batch(
                    prepared,
                    deadline_monotonic=deadline,
                )
            except (EventIdConflict, EventReconciliationRequired):
                raise
            except BaseException:
                self._latch_ledger_reconciliation_required()
                raise
            if committed is None:
                raise EventCommitError(
                    "Stable event batch is absent from the durable ledger",
                    commit_outcome="none",
                    deadline_monotonic=deadline,
                )
            return self._reconcile_confirmed_attempt(
                committed,
                catch_up_through_sequence=high_water,
                state=state,
                await_delivery=False,
                enqueue_publication=False,
            ).result

    def register_committed_reducer(
        self,
        *,
        reducer_id: str,
        through_sequence: int,
        apply_committed: Callable[[tuple[AgentEvent, ...]], None],
        rebuild_committed: Callable[[tuple[AgentEvent, ...]], None] | None = None,
    ) -> None:
        with self.write_coordinator.lock:
            if reducer_id in self._committed_reducers:
                raise ValueError(f"Committed reducer already registered: {reducer_id}")
            registration = _CommittedReducerRegistration(
                reducer_id=reducer_id,
                through_sequence=through_sequence,
                apply_committed=apply_committed,
                rebuild_committed=rebuild_committed,
            )
            # Registration is durable process state even when initial catch-up
            # fails. Keeping the failed registration is what makes an explicit
            # full reconciliation possible without reopening the session.
            self._committed_reducers[reducer_id] = registration
            try:
                current_last = self.event_log.next_sequence() - 1
                missing = _contiguous_interval(
                    self.event_log.iter(after_sequence=through_sequence),
                    start=through_sequence + 1,
                    end=current_last,
                )
                if missing:
                    apply_committed(missing)
                registration.through_sequence = current_last
            except Exception:
                registration.reconciliation_required = True
                registration.last_error = "initial committed reducer catch-up failed"
                self._reconciliation_required = True
                raise

    def reconcile_committed_reducer(self, reducer_id: str) -> None:
        with self.write_coordinator.lock:
            registration = self._committed_reducers[reducer_id]
            try:
                events = tuple(self.event_log.iter())
                if registration.rebuild_committed is not None:
                    registration.rebuild_committed(events)
                else:
                    missing = tuple(
                        event
                        for event in events
                        if event.sequence is not None
                        and event.sequence > registration.through_sequence
                    )
                    if missing:
                        registration.apply_committed(missing)
                registration.through_sequence = events[-1].sequence if events else 0  # type: ignore[assignment]
            except Exception:
                registration.reconciliation_required = True
                registration.last_error = "committed reducer rebuild failed"
                self._reconciliation_required = True
                raise
            registration.reconciliation_required = False
            registration.last_error = None
            self._reconciliation_required = any(
                item.reconciliation_required
                for item in self._committed_reducers.values()
            )

    def unregister_committed_reducer(self, reducer_id: str) -> None:
        """Detach one process-local reducer without changing durable truth."""

        with self.write_coordinator.lock:
            self._committed_reducers.pop(reducer_id, None)
            self._reconciliation_required = any(
                item.reconciliation_required
                for item in self._committed_reducers.values()
            )

    async def write_event(
        self,
        event: AgentEvent,
        *,
        expected_last_sequence: int | None = None,
        state: LoopState | None = None,
    ) -> EventWriteResult:
        return await self.write_events(
            (event,),
            expected_last_sequence=expected_last_sequence,
            state=state,
        )

    async def write_events(
        self,
        events: Sequence[AgentEvent],
        *,
        expected_last_sequence: int | None = None,
        state: LoopState | None = None,
    ) -> EventWriteResult:
        self.publisher.bind_running_loop()
        prepared = self._prepare_event_batch(events)
        deadline = self.event_write_service.new_deadline_monotonic()
        try:
            attempt = await self.event_write_service.execute(
                lambda: self._commit_reduce_enqueue(
                    prepared,
                    expected_last_sequence=expected_last_sequence,
                    state=state,
                    await_delivery=True,
                    deadline_monotonic=deadline,
                ),
                deadline_monotonic=deadline,
            )
        except PendingRuntimeEventWriteError as error:
            raise EventCommitError(
                str(error),
                commit_outcome="none",
                deadline_monotonic=deadline,
            ) from error
        except RuntimeEventWriteCancelled as cancelled:
            outcome = _cancelled_event_commit_outcome(cancelled)
            if outcome.status == "unknown":
                self.latch_event_commit_outcome_unknown()
            raise EventWriteCancelled(outcome) from cancelled
        try:
            publication_errors = await _await_publication(
                attempt.published_events,
                attempt.delivery_futures,
            )
        except asyncio.CancelledError as cancelled:
            raise EventWriteCancelled(
                EventBatchCommitOutcome(
                    status="full",
                    deadline_monotonic=deadline,
                    result=replace(
                        attempt.result,
                        publication_status="enqueued",
                    ),
                )
            ) from cancelled
        return replace(
            attempt.result,
            publication_status=(
                "completed"
                if attempt.result.publication_status != "unavailable"
                else "unavailable"
            ),
            publication_errors=(
                *attempt.result.publication_errors,
                *publication_errors,
            ),
        )

    def write_events_from_thread(
        self,
        events: Sequence[AgentEvent],
        *,
        expected_last_sequence: int | None = None,
        state: LoopState | None = None,
    ) -> EventWriteResult:
        prepared = self._prepare_event_batch(events)
        deadline = (
            self.event_write_service.current_deadline_monotonic()
            if self.event_write_service.is_current_owner()
            else self.event_write_service.new_deadline_monotonic()
        )
        return self.event_write_service.execute_blocking(
            lambda: self._commit_reduce_enqueue(
                prepared,
                expected_last_sequence=expected_last_sequence,
                state=state,
                await_delivery=False,
                deadline_monotonic=deadline,
            ).result,
            deadline_monotonic=deadline,
        )

    def commit_reduce_events_from_thread(
        self,
        events: Sequence[AgentEvent],
        *,
        expected_last_sequence: int | None = None,
        state: LoopState | None = None,
    ) -> EventWriteResult:
        """Commit and synchronously fold while deferring ordered publication."""

        prepared = self._prepare_event_batch(events)
        deadline = (
            self.event_write_service.current_deadline_monotonic()
            if self.event_write_service.is_current_owner()
            else self.event_write_service.new_deadline_monotonic()
        )
        return self.event_write_service.execute_blocking(
            lambda: self._commit_reduce_enqueue(
                prepared,
                expected_last_sequence=expected_last_sequence,
                state=state,
                await_delivery=False,
                enqueue_publication=False,
                deadline_monotonic=deadline,
            ).result,
            deadline_monotonic=deadline,
        )

    def publish_committed_through_from_thread(
        self,
        *,
        through_sequence: int,
        state: LoopState | None = None,
    ) -> Literal["completed", "enqueued", "unavailable"]:
        """Enqueue a contiguous committed prefix after control linearization."""

        with self.write_coordinator.lock:
            enqueue = self._catch_up_publisher(
                through_sequence=through_sequence,
                state=state,
                await_delivery=False,
            )
        return _publication_status(enqueue)

    def _prepare_event_batch(
        self, events: Sequence[AgentEvent]
    ) -> tuple[AgentEvent, ...]:
        return tuple(self.prepare_event_for_write(event) for event in events)

    def prepare_event_for_write(self, event: AgentEvent) -> AgentEvent:
        """Return the exact event value owned by this session's write boundary."""

        self._require_runtime_managed_sequence(event)
        return self._with_default_metadata(event)

    def _validate_run_lifecycle_batch(self, events: tuple[AgentEvent, ...]) -> None:
        """Enforce the stable RunStart-to-RunEnd identity before commit."""

        starts_in_batch = {
            event.run_id: event for event in events if isinstance(event, RunStartEvent)
        }
        if len(starts_in_batch) != sum(
            isinstance(event, RunStartEvent) for event in events
        ):
            raise ValueError("event batch contains duplicate RunStart facts")
        ends_in_batch: set[str] = set()
        for terminal in (event for event in events if isinstance(event, RunEndEvent)):
            if terminal.run_id in ends_in_batch:
                raise ValueError("event batch contains duplicate RunEnd facts")
            ends_in_batch.add(terminal.run_id)
            candidate_start = starts_in_batch.get(terminal.run_id)
            stored_start = self.long_horizon_state_store.run_start(terminal.run_id)
            if candidate_start is not None and stored_start is not None:
                if not _same_event_candidate(stored_start, candidate_start):
                    raise ValueError("RunStart retry payload conflicts with ledger")
            start = candidate_start or stored_start
            if start is None:
                lifecycle = self.event_log.read_raw_events_by_types(
                    (EventType.RUN_START.value,),
                    run_ids=(terminal.run_id,),
                    max_events=2,
                    max_payload_bytes=512 * 1024,
                    deadline_monotonic=monotonic() + 5.0,
                )
                decoded = tuple(
                    item.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
                    for item in lifecycle.events
                )
                starts = tuple(
                    item for item in decoded if isinstance(item, RunStartEvent)
                )
                start = starts[0] if len(starts) == 1 else None
            if start is None:
                raise ValueError("RunEnd requires exactly one durable RunStart")
            if terminal.id != start.terminal_run_end_event_id:
                raise ValueError("RunEnd id does not match RunStart terminal contract")
            existing_end = self.event_log.get_by_id(terminal.id)
            if existing_end is not None and not (
                isinstance(existing_end, RunEndEvent)
                and _same_event_candidate(existing_end, terminal)
            ):
                raise ValueError("run already has a conflicting durable RunEnd")

        compaction_starts = {
            event.id: event
            for event in events
            if isinstance(event, ContextCompactionStartedEvent)
        }
        compaction_terminals = tuple(
            event
            for event in events
            if isinstance(
                event,
                (ContextCompactionCompletedEvent, ContextCompactionFailedEvent),
            )
            and event.started_event_id is not None
        )
        window_compaction_starts = {
            event.id: event
            for event in events
            if isinstance(event, ContextWindowCompactionStartedEvent)
        }
        window_compaction_terminals = tuple(
            event
            for event in events
            if isinstance(
                event,
                (
                    ContextWindowCompactionCompletedEvent,
                    ContextWindowCompactionFailedEvent,
                ),
            )
        )
        from pulsara_agent.event_log.serialization import (
            DEFAULT_EVENT_SCHEMA_REGISTRY,
        )

        graph_events = tuple(
            (event, binding.schema_contract)
            for event in events
            if (
                binding := DEFAULT_EVENT_SCHEMA_REGISTRY.resolve_for_event(event)
            ).schema_contract.event_domain
            == "subagent_graph"
        )
        resume_boundaries = tuple(
            event
            for event in events
            if isinstance(event, RunInteractionResumeBoundaryEvent)
        )
        if (
            not compaction_terminals
            and not window_compaction_starts
            and not window_compaction_terminals
            and not graph_events
            and not resume_boundaries
        ):
            return

        for terminal in compaction_terminals:
            candidate_start = compaction_starts.get(terminal.started_event_id)
            existing_start = (
                self.event_log.get_by_id(terminal.started_event_id)
                if candidate_start is None
                else None
            )
            started = candidate_start or (
                existing_start
                if isinstance(existing_start, ContextCompactionStartedEvent)
                else None
            )
            if started is None:
                raise ValueError(
                    "compaction terminal requires exactly one matching Started"
                )
            if (
                terminal.id != started.terminal_event_id
                or terminal.compaction_id != started.compaction_id
                or terminal.host_boundary_id != started.host_boundary_id
                or terminal.host_boundary_kind != started.host_boundary_kind
            ):
                raise ValueError("compaction terminal pairing contract mismatch")
            existing_terminal = self.event_log.get_by_id(started.terminal_event_id)
            if existing_terminal is not None and not _same_event_candidate(
                existing_terminal, terminal
            ):
                raise ValueError("compaction Started already has a terminal fact")

        candidate_by_id = {event.id: event for event in events}
        for started in window_compaction_starts.values():
            chain = self.long_horizon_state_store.window_state(started.run_id)
            projection = self.long_horizon_state_store.projection_state(
                started.plan.source_window_id
            )
            if (
                chain is None
                or chain.active_window_id != started.plan.source_window_id
                or projection is None
                or projection.projection_generation
                != started.plan.source_projection_generation
                or projection.state_semantic_fingerprint
                != started.plan.source_projection_state_fingerprint
            ):
                raise ValueError(
                    "window compaction Started source is not the active projection"
                )
        for terminal in window_compaction_terminals:
            if terminal.started_event_id is None:
                if not isinstance(terminal, ContextWindowCompactionFailedEvent):
                    raise ValueError(
                        "window compaction Completed requires a matching Started"
                    )
                continue
            started = window_compaction_starts.get(terminal.started_event_id)
            if started is None:
                existing_started = next(
                    (
                        item
                        for item in self.long_horizon_state_store.pending_window_compactions()
                        if item.id == terminal.started_event_id
                    ),
                    None,
                )
                if existing_started is None:
                    existing_started = self.event_log.get_by_id(
                        terminal.started_event_id
                    )
                started = (
                    existing_started
                    if isinstance(
                        existing_started, ContextWindowCompactionStartedEvent
                    )
                    else None
                )
            if (
                started is None
                or terminal.compaction_id != started.plan.compaction_id
                or terminal.plan_fingerprint != started.plan.plan_fingerprint
            ):
                raise ValueError(
                    "window compaction terminal does not match its Started plan"
                )
            existing_terminal = self.event_log.get_by_id(terminal.id)
            if existing_terminal is not None and not _same_event_candidate(
                existing_terminal, terminal
            ):
                raise ValueError(
                    "window compaction Started already has a terminal fact"
                )
            if isinstance(terminal, ContextWindowCompactionCompletedEvent):
                settlement = candidate_by_id.get(terminal.rollout_settlement_event_id)
                if settlement is None:
                    settlement = self.event_log.get_by_id(
                        terminal.rollout_settlement_event_id
                    )
                closed = candidate_by_id.get(terminal.source_window_close_event_id)
                opened = candidate_by_id.get(terminal.target_window_open_event_id)
                event_positions = {
                    event.id: index for index, event in enumerate(events)
                }
                expected_order = (
                    event_positions.get(terminal.id),
                    event_positions.get(terminal.source_window_close_event_id),
                    event_positions.get(terminal.target_window_open_event_id),
                )
                reservation_quote = (
                    started.plan.rollout_reservation.model_call_reservation_quote
                )
                if (
                    terminal.id != started.plan.stable_completed_event_id
                    or terminal.source_window_close_event_id
                    != started.plan.stable_source_window_close_event_id
                    or terminal.target_window_open_event_id
                    != started.plan.stable_target_window_open_event_id
                    or terminal.summarizer_call.resolved_model_call_id
                    != started.plan.summarizer_call.resolved_model_call_id
                    or terminal.summarizer_call.target.target_fingerprint
                    != started.plan.summarizer_call.target.target_fingerprint
                    or not isinstance(
                        settlement, RolloutBudgetReservationSettledEvent
                    )
                    or settlement.reservation_id
                    != started.plan.rollout_reservation.reservation_id
                    or settlement.usage_charge is None
                    or reservation_quote is None
                    or settlement.usage_charge.reservation_quote_fact_fingerprint
                    != reservation_quote.quote_fact_fingerprint
                    or not isinstance(closed, ContextWindowClosedEvent)
                    or closed.id
                    != started.plan.stable_source_window_close_event_id
                    or closed.window_id != started.plan.source_window_id
                    or closed.window_generation
                    != started.plan.source_window_generation
                    or closed.final_projection_generation
                    != started.plan.source_projection_generation
                    or closed.final_projection_state_fingerprint
                    != started.plan.source_projection_state_fingerprint
                    or closed.source_through_sequence
                    != started.plan.source_through_sequence
                    or closed.next_window_id != started.plan.target_window_id
                    or closed.compaction_terminal_event_id != terminal.id
                    or not isinstance(opened, ContextWindowOpenedEvent)
                    or opened.id != started.plan.stable_target_window_open_event_id
                    or opened.window.window_id != started.plan.target_window_id
                    or opened.window.generation
                    != started.plan.target_window_generation
                    or opened.window.source_compaction_id
                    != terminal.compaction_id
                    or opened.opening_batch_id != terminal.compaction_id
                    or any(position is None for position in expected_order)
                    or expected_order[1] != expected_order[0] + 1
                    or expected_order[2] != expected_order[1] + 1
                ):
                    raise ValueError(
                        "window compaction success batch is incomplete or mismatched"
                    )
            else:
                if (
                    terminal.id != started.plan.stable_failed_event_id
                    or terminal.compaction_attempt_index
                    != started.plan.compaction_attempt_index
                    or terminal.source_window_id != started.plan.source_window_id
                    or terminal.source_window_generation
                    != started.plan.source_window_generation
                ):
                    raise ValueError(
                        "window compaction failure does not match its Started plan"
                    )
        run_starts_by_run_id = dict(starts_in_batch)
        for event, schema in graph_events:
            start = run_starts_by_run_id.get(event.run_id)
            if start is None:
                start = self.long_horizon_state_store.run_start(event.run_id)
            if start is None:
                snapshot = self.event_log.read_raw_events_by_types(
                    (EventType.RUN_START.value,),
                    run_ids=(event.run_id,),
                    max_events=2,
                    max_payload_bytes=512 * 1024,
                    deadline_monotonic=monotonic() + 5.0,
                )
                decoded = tuple(
                    item.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
                    for item in snapshot.events
                )
                matching = tuple(
                    item for item in decoded if isinstance(item, RunStartEvent)
                )
                start = matching[0] if len(matching) == 1 else None
                if start is not None:
                    run_starts_by_run_id[event.run_id] = start
            if start is None:
                raise ValueError(
                    "graph-domain event requires an owning durable RunStart contract"
                )
            supported = tuple(
                item
                for item in start.subagent_graph_reducer_contract.supported_graph_events
                if item.event_type == str(event.type)
                and item.event_schema_version == schema.event_schema_version
            )
            if (
                len(supported) != 1
                or supported[0].event_schema_fingerprint
                != schema.event_schema_fingerprint
                or supported[0].event_domain_contract_fingerprint
                != schema.domain_contract_fingerprint
            ):
                raise ValueError(
                    "graph-domain event is unsupported by the owning RunStart reducer contract"
                )
        candidate_index = {event.id: index for index, event in enumerate(events)}
        exposure_candidates = {
            event.exposure.exposure_id: event
            for event in events
            if isinstance(event, CapabilityExposureResolvedEvent)
        }
        for boundary_event in resume_boundaries:
            boundary = boundary_event.boundary
            start = candidate_by_id.get(boundary.original_run_start_event_id)
            if start is None:
                start = self.event_log.get_by_id(
                    boundary.original_run_start_event_id
                )
            if (
                not isinstance(start, RunStartEvent)
                or start.sequence != boundary.original_run_start_sequence
                or start.run_id != boundary_event.run_id
            ):
                raise ValueError("resume boundary does not reference its Host RunStart")
            if (
                start.permission_snapshot_id != boundary.permission_snapshot_id
                or start.model_target.target_fingerprint
                != boundary.model_target_fingerprint
            ):
                raise ValueError("resume boundary changed a run-frozen contract")
            source_snapshot = self.event_log.read_raw_events_by_types(
                (EventType.CAPABILITY_EXPOSURE_RESOLVED.value,),
                run_ids=(boundary_event.run_id,),
                max_events=128,
                max_payload_bytes=4 * 1024 * 1024,
                deadline_monotonic=monotonic() + 5.0,
            )
            source_exposure = next(
                (
                    decoded
                    for item in source_snapshot.events
                    if isinstance(
                        decoded := item.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY),
                        CapabilityExposureResolvedEvent,
                    )
                    and decoded.exposure.exposure_id
                    == boundary.source_exposure_id
                ),
                None,
            )
            if (
                source_exposure is None
                or source_exposure.exposure.exposure_semantic_fingerprint
                != boundary.source_exposure_semantic_fingerprint
                or source_exposure.exposure.exposure_fact_fingerprint
                != boundary.source_exposure_fact_fingerprint
            ):
                raise ValueError("resume boundary source exposure is unavailable")
            effective = exposure_candidates.get(boundary.effective_exposure_id)
            if (
                effective is None
                or candidate_index[effective.id] >= candidate_index[boundary_event.id]
                or effective.run_id != boundary_event.run_id
                or effective.exposure.exposure_semantic_fingerprint
                != boundary.effective_exposure_semantic_fingerprint
                or effective.exposure.exposure_fact_fingerprint
                != boundary.effective_exposure_fact_fingerprint
                or effective.exposure.owner.owner_id != boundary.identity.boundary_id
            ):
                raise ValueError(
                    "resume boundary effective exposure must precede it in the batch"
                )
            _validate_continuation_exposure_subset(
                source_exposure.exposure,
                effective.exposure,
            )
            expected_transition = (
                "reused"
                if effective.exposure.resolution_kind == "continuation_reused"
                else "narrowed"
            )
            if boundary.exposure_transition != expected_transition:
                raise ValueError("resume boundary exposure transition mismatch")
            for audit_id in boundary.committed_mcp_audit_event_ids:
                audit = next(
                    (
                        event
                        for event in events
                        if event.id == audit_id
                        and isinstance(event, McpCapabilitySnapshotInstalledEvent)
                    ),
                    None,
                )
                if (
                    audit is None
                    or candidate_index[audit.id] >= candidate_index[effective.id]
                ):
                    raise ValueError(
                        "resume boundary MCP audits must precede its exposure"
                    )

    def _commit_reduce_enqueue(
        self,
        events: tuple[AgentEvent, ...],
        *,
        expected_last_sequence: int | None,
        state: LoopState | None,
        await_delivery: bool,
        deadline_monotonic: float,
        enqueue_publication: bool = True,
    ) -> _WriteAttempt:
        if not events:
            result = EventWriteResult(
                committed_events=(),
                commit_status="committed",
                reducer_high_waters={
                    key: reducer.through_sequence
                    for key, reducer in self._committed_reducers.items()
                },
                reconciliation_required=self.reconciliation_required,
                reducer_errors=(),
                publication_status="completed" if await_delivery else "enqueued",
                publisher_enqueued_through_sequence=self.publisher.enqueued_through_sequence,
            )
            return _WriteAttempt(
                result=result, delivery_futures=(), published_events=()
            )
        with self.write_coordinator.lock:
            if self.reconciliation_required:
                failed = tuple(
                    f"{item.reducer_id}: {item.last_error or 'reconciliation required'}"
                    for item in self._committed_reducers.values()
                    if item.reconciliation_required
                )
                raise EventReconciliationRequired(
                    "RuntimeSession ledger or committed reducer requires reconciliation"
                    + (f" ({'; '.join(failed)})" if failed else "")
                )
            self._validate_run_lifecycle_batch(events)
            self.long_horizon_state_store.validate_next_batch(events)
            commit_deadline = _commit_phase_deadline(deadline_monotonic)
            try:
                committed = tuple(
                    self.event_log.extend(
                        events,
                        expected_last_sequence=expected_last_sequence,
                        deadline_monotonic=commit_deadline,
                    )
                )
            except EventLogWriteConflict as exc:
                confirmed, confirmed_high_water = self._confirm_committed_batch(
                    events,
                    deadline_monotonic=deadline_monotonic,
                )
                if confirmed is not None:
                    return self._reconcile_confirmed_attempt(
                        confirmed,
                        catch_up_through_sequence=max(
                            confirmed_high_water,
                            exc.actual_last_sequence,
                        ),
                        state=state,
                        await_delivery=await_delivery,
                        enqueue_publication=enqueue_publication,
                    )
                self._catch_up_reducers(exc.actual_last_sequence)
                self._catch_up_publisher(
                    through_sequence=exc.actual_last_sequence,
                    state=state,
                    await_delivery=await_delivery,
                )
                raise EventWriteConflict(
                    runtime_session_id=self.runtime_session_id,
                    expected_last_sequence=exc.expected_last_sequence,
                    actual_last_sequence=exc.actual_last_sequence,
                    deadline_monotonic=deadline_monotonic,
                ) from exc
            except Exception as exc:
                try:
                    confirmed, confirmed_high_water = self._confirm_committed_batch(
                        events,
                        deadline_monotonic=deadline_monotonic,
                    )
                except (EventIdConflict, EventReconciliationRequired):
                    raise
                except Exception as confirmation_error:
                    self._latch_ledger_reconciliation_required()
                    raise EventCommitError(
                        f"Event batch commit confirmation failed: {type(confirmation_error).__name__}",
                        commit_outcome="unknown",
                        deadline_monotonic=deadline_monotonic,
                    ) from confirmation_error
                if confirmed is not None:
                    return self._reconcile_confirmed_attempt(
                        confirmed,
                        catch_up_through_sequence=confirmed_high_water,
                        state=state,
                        await_delivery=await_delivery,
                        enqueue_publication=enqueue_publication,
                    )
                raise EventCommitError(
                    f"Event batch commit failed: {type(exc).__name__}",
                    commit_outcome="none",
                    deadline_monotonic=deadline_monotonic,
                ) from exc

            first_sequence = _event_sequence(committed[0])
            last_sequence = _event_sequence(committed[-1])
            reducer_errors: list[CommittedReducerError] = []
            for registration in self._committed_reducers.values():
                try:
                    if registration.through_sequence < first_sequence - 1:
                        missing = _contiguous_interval(
                            self.event_log.iter(
                                after_sequence=registration.through_sequence
                            ),
                            start=registration.through_sequence + 1,
                            end=first_sequence - 1,
                        )
                        registration.apply_committed(missing)
                        registration.through_sequence = first_sequence - 1
                    registration.apply_committed(committed)
                    registration.through_sequence = last_sequence
                except Exception as exc:
                    registration.reconciliation_required = True
                    registration.last_error = (
                        f"{type(exc).__name__}: {_bounded_error(exc)}"
                    )
                    self._reconciliation_required = True
                    reducer_errors.append(
                        CommittedReducerError(
                            reducer_id=registration.reducer_id,
                            error_type=type(exc).__name__,
                            message=_bounded_error(exc),
                        )
                    )

            publication_errors: tuple[EventPublicationError, ...] = ()
            publisher_events: tuple[AgentEvent, ...] = ()
            if not enqueue_publication:
                enqueue = PublisherEnqueueResult(
                    status="unavailable",
                    enqueued_through_sequence=self.publisher.enqueued_through_sequence,
                )
            else:
                try:
                    publisher_events = self._publisher_catch_up_events(
                        current=committed,
                        first_sequence=first_sequence,
                    )
                except EventReconciliationRequired as exc:
                    gap_error = PublisherSequenceGapError(
                        "runtime publisher committed sequence interval is unavailable"
                    )
                    gap_error.__cause__ = exc
                    self.publisher.errors.append(gap_error)
                    publication_errors = (
                        EventPublicationError(
                            event_id=committed[0].id,
                            sequence=first_sequence,
                            subscriber_id="runtime_publisher",
                            error_type="PublisherSequenceGapError",
                            message=str(gap_error),
                        ),
                    )
                    enqueue = PublisherEnqueueResult(
                        status="unavailable",
                        enqueued_through_sequence=(
                            self.publisher.enqueued_through_sequence
                        ),
                    )
                else:
                    enqueue = self.publisher.enqueue_committed_batch(
                        tuple(
                            RuntimePublishedEvent(
                                runtime_session_id=self.runtime_session_id,
                                event=event,
                                state=state,
                            )
                            for event in publisher_events
                        ),
                        await_delivery=await_delivery,
                    )
            result = EventWriteResult(
                committed_events=committed,
                commit_status="committed",
                reducer_high_waters={
                    key: registration.through_sequence
                    for key, registration in self._committed_reducers.items()
                },
                reconciliation_required=self.reconciliation_required,
                reducer_errors=tuple(reducer_errors),
                publication_status=_publication_status(enqueue),
                publisher_enqueued_through_sequence=enqueue.enqueued_through_sequence,
                publication_errors=publication_errors,
            )
            return _WriteAttempt(
                result=result,
                delivery_futures=enqueue.delivery_futures,
                published_events=publisher_events,
            )

    def _confirm_committed_batch(
        self,
        candidates: tuple[AgentEvent, ...],
        *,
        deadline_monotonic: float,
    ) -> tuple[tuple[AgentEvent, ...] | None, int]:
        confirmation = self.event_log.confirm_batch(
            candidates,
            deadline_monotonic=deadline_monotonic,
        )
        stored = confirmation.committed_events
        if confirmation.missing_event_ids:
            if stored:
                self._latch_ledger_reconciliation_required()
                raise EventReconciliationRequired(
                    "Only part of an atomic event batch can be confirmed by id"
                )
            return None, confirmation.actual_last_sequence
        sequences = [_event_sequence(event) for event in stored]
        if sequences != list(range(sequences[0], sequences[-1] + 1)):
            self._latch_ledger_reconciliation_required()
            raise EventReconciliationRequired(
                f"Confirmed event batch is not contiguous: {sequences}"
            )
        return stored, confirmation.actual_last_sequence

    def _latch_ledger_reconciliation_required(self) -> None:
        self._ledger_reconciliation_required = True

    def _reconcile_confirmed_attempt(
        self,
        committed: tuple[AgentEvent, ...],
        *,
        catch_up_through_sequence: int,
        state: LoopState | None,
        await_delivery: bool,
        enqueue_publication: bool = True,
    ) -> _WriteAttempt:
        target_sequence = max(
            _event_sequence(committed[-1]),
            catch_up_through_sequence,
        )
        self._catch_up_reducers(target_sequence)
        reducer_errors = tuple(
            CommittedReducerError(
                reducer_id=registration.reducer_id,
                error_type="EventReconciliationRequired",
                message="Committed reducer could not catch up an idempotently confirmed event batch",
            )
            for registration in self._committed_reducers.values()
            if registration.reconciliation_required
        )

        publisher_start = self.publisher.enqueued_through_sequence + 1
        publisher_events: tuple[AgentEvent, ...] = ()
        publication_errors: tuple[EventPublicationError, ...] = ()
        if not enqueue_publication:
            enqueue = PublisherEnqueueResult(
                status="unavailable",
                enqueued_through_sequence=self.publisher.enqueued_through_sequence,
            )
        elif publisher_start > target_sequence:
            enqueue = PublisherEnqueueResult(
                status="completed" if await_delivery else "enqueued",
                enqueued_through_sequence=self.publisher.enqueued_through_sequence,
            )
        else:
            try:
                publisher_events = _contiguous_interval(
                    self.event_log.iter(after_sequence=publisher_start - 1),
                    start=publisher_start,
                    end=target_sequence,
                )
            except EventReconciliationRequired as exc:
                gap_error = PublisherSequenceGapError(
                    "runtime publisher confirmed sequence interval is unavailable"
                )
                gap_error.__cause__ = exc
                self.publisher.errors.append(gap_error)
                publication_errors = (
                    EventPublicationError(
                        event_id=committed[0].id,
                        sequence=_event_sequence(committed[0]),
                        subscriber_id="runtime_publisher",
                        error_type="PublisherSequenceGapError",
                        message=str(gap_error),
                    ),
                )
                enqueue = PublisherEnqueueResult(
                    status="unavailable",
                    enqueued_through_sequence=self.publisher.enqueued_through_sequence,
                )
            else:
                enqueue = self.publisher.enqueue_committed_batch(
                    tuple(
                        RuntimePublishedEvent(
                            runtime_session_id=self.runtime_session_id,
                            event=event,
                            state=state,
                        )
                        for event in publisher_events
                    ),
                    await_delivery=await_delivery,
                )
        result = EventWriteResult(
            committed_events=committed,
            commit_status="committed",
            reducer_high_waters={
                key: registration.through_sequence
                for key, registration in self._committed_reducers.items()
            },
            reconciliation_required=self.reconciliation_required,
            reducer_errors=reducer_errors,
            publication_status=_publication_status(enqueue),
            publisher_enqueued_through_sequence=enqueue.enqueued_through_sequence,
            publication_errors=publication_errors,
        )
        return _WriteAttempt(
            result=result,
            delivery_futures=enqueue.delivery_futures,
            published_events=publisher_events,
        )

    def _catch_up_reducers(self, through_sequence: int) -> None:
        for registration in self._committed_reducers.values():
            if registration.through_sequence >= through_sequence:
                continue
            missing = _contiguous_interval(
                self.event_log.iter(after_sequence=registration.through_sequence),
                start=registration.through_sequence + 1,
                end=through_sequence,
            )
            try:
                registration.apply_committed(missing)
                registration.through_sequence = through_sequence
            except Exception:
                registration.reconciliation_required = True
                self._reconciliation_required = True

    def _catch_up_publisher(
        self,
        *,
        through_sequence: int,
        state: LoopState | None,
        await_delivery: bool,
    ) -> PublisherEnqueueResult:
        start = self.publisher.enqueued_through_sequence + 1
        if start > through_sequence:
            return PublisherEnqueueResult(
                status="completed" if await_delivery else "enqueued",
                enqueued_through_sequence=self.publisher.enqueued_through_sequence,
            )
        try:
            events = _contiguous_interval(
                self.event_log.iter(after_sequence=start - 1),
                start=start,
                end=through_sequence,
            )
        except EventReconciliationRequired as exc:
            error = PublisherSequenceGapError(
                "runtime publisher committed sequence interval is unavailable"
            )
            error.__cause__ = exc
            self.publisher.errors.append(error)
            return PublisherEnqueueResult(
                status="unavailable",
                enqueued_through_sequence=self.publisher.enqueued_through_sequence,
            )
        return self.publisher.enqueue_committed_batch(
            tuple(
                RuntimePublishedEvent(
                    runtime_session_id=self.runtime_session_id,
                    event=event,
                    state=state,
                )
                for event in events
            ),
            await_delivery=await_delivery,
        )

    def _publisher_catch_up_events(
        self,
        *,
        current: tuple[AgentEvent, ...],
        first_sequence: int,
    ) -> tuple[AgentEvent, ...]:
        start = self.publisher.enqueued_through_sequence + 1
        prefix: tuple[AgentEvent, ...] = ()
        if start < first_sequence:
            prefix = _contiguous_interval(
                self.event_log.iter(after_sequence=start - 1),
                start=start,
                end=first_sequence - 1,
            )
        return prefix + current

    async def emit(
        self, event: AgentEvent, *, state: LoopState | None = None
    ) -> AgentEvent:
        result = await self.write_event(event, state=state)
        if result.publication_errors:
            raise EventPublicationAfterCommitError(result)
        return result.committed_events[0]

    async def emit_many(
        self,
        events: Iterable[AgentEvent],
        *,
        state: LoopState | None = None,
    ) -> list[AgentEvent]:
        result = await self.write_events(tuple(events), state=state)
        if result.publication_errors:
            raise EventPublicationAfterCommitError(result)
        return list(result.committed_events)

    def emit_from_thread(
        self, event: AgentEvent, *, state: LoopState | None = None
    ) -> AgentEvent:
        result = self.write_events_from_thread((event,), state=state)
        return result.committed_events[0]

    def publish_stored_event(
        self, event: AgentEvent, *, state: LoopState | None = None
    ) -> None:
        if event.sequence is None:
            raise ValueError("Stored events must have a canonical sequence")
        self._require_default_metadata_present(event)
        self.publish_stored_events((event,), state=state)

    def publish_stored_events(
        self,
        events: Iterable[AgentEvent],
        *,
        state: LoopState | None = None,
    ) -> None:
        event_list = tuple(events)
        for event in event_list:
            if event.sequence is None:
                raise ValueError("Stored events must have a canonical sequence")
            self._require_default_metadata_present(event)
        if not event_list:
            return
        with self.write_coordinator.lock:
            target = max(_event_sequence(event) for event in event_list)
            self._catch_up_reducers(target)
            start = self.publisher.enqueued_through_sequence + 1
            if start > target:
                return
            publish_events = _contiguous_interval(
                self.event_log.iter(after_sequence=start - 1),
                start=start,
                end=target,
            )
            self.publisher.enqueue_committed_batch(
                tuple(
                    RuntimePublishedEvent(
                        runtime_session_id=self.runtime_session_id,
                        event=stored,
                        state=state,
                    )
                    for stored in publish_events
                ),
                await_delivery=False,
            )

    def make_thread_recorder(
        self, *, state: LoopState | None = None
    ) -> RuntimeThreadRecorder:
        return RuntimeThreadRecorder(runtime_session=self, state=state)

    def close(self) -> None:
        # Owned-local: we shut the manager down. Borrowed (HostCore path): we do
        # NOT kill/detach/shutdown the shared manager here — lease release is the
        # supervisor/HostCore job and must run exactly once (contract §5).
        # Idempotent: shutting an already-shut manager down is a no-op.
        self.context_input_manifest_service.close_if_idle()
        self.context_input_io_service.close_if_idle()
        self.event_write_service.close_if_idle()
        self.subagent_graph_checkpoint_service.close_if_idle()
        if self.window_compaction_service is not None:
            self.window_compaction_service.close_if_idle()
        self.context_static_instruction_cache.clear()
        self.context_candidate_lifecycle_cache.clear()
        self.tool_result_render_cache.clear()
        self._context_input_cache_diagnostics.clear()
        subagent_runtime = self.subagent_runtime
        detach = getattr(subagent_runtime, "detach_from_parent_session", None)
        if (
            callable(detach)
            and getattr(subagent_runtime, "parent_runtime_session", None) is self
        ):
            detach()
        with self.write_coordinator.lock:
            self._committed_reducers.clear()
            self._reconciliation_required = False
            self._ledger_reconciliation_required = False
            self._context_input_reconciliation_required = False
        if self._owns_terminal_manager:
            self.terminal_sessions.shutdown()

    def create_tool_executor(
        self,
        *,
        record_event: RuntimeThreadRecorder | None = None,
        memory_proposal_sink: MemoryProposalSink | None = None,
        memory_recall_service=None,
        memory_query=None,
        graph_id: str | None = None,
        memory_read_scopes: frozenset[str] | None = None,
        permission_state: PermissionState | None = None,
    ):
        from pulsara_agent.tools import ToolExecutor
        from pulsara_agent.tools.builtins.registry import build_core_tool_registry

        if record_event is not None and not isinstance(
            record_event, RuntimeThreadRecorder
        ):
            raise TypeError(
                "create_tool_executor(record_event=...) requires RuntimeSession.make_thread_recorder(...)"
            )

        return ToolExecutor(
            registry=build_core_tool_registry(
                self,
                memory_proposal_sink=memory_proposal_sink,
                memory_recall_service=memory_recall_service,
                memory_query=memory_query,
                graph_id=graph_id,
                memory_read_scopes=memory_read_scopes,
                permission_state=permission_state,
                extra_tools=self.extra_tool_bindings,
            ),
            record_event=record_event,
            artifact_service=self.artifact_service,
            runtime_session_id=self.runtime_session_id,
        )


def _commit_phase_deadline(terminal_deadline_monotonic: float) -> float:
    """Reserve the final fifth of a write owner for stable confirmation."""

    now = monotonic()
    remaining = terminal_deadline_monotonic - now
    if remaining <= 0:
        raise TimeoutError("event-write deadline exceeded before commit")
    return now + (remaining * 0.8)


def _cancelled_event_commit_outcome(
    cancelled: RuntimeEventWriteCancelled,
) -> EventBatchCommitOutcome:
    result = cancelled.operation_result
    if isinstance(result, _WriteAttempt):
        return EventBatchCommitOutcome(
            status="full",
            deadline_monotonic=cancelled.deadline_monotonic,
            result=result.result,
        )
    if isinstance(result, EventWriteResult):
        return EventBatchCommitOutcome(
            status="full",
            deadline_monotonic=cancelled.deadline_monotonic,
            result=result,
        )
    error = cancelled.operation_error
    if isinstance(error, EventCommitError):
        return EventBatchCommitOutcome(
            status=error.commit_outcome,
            deadline_monotonic=cancelled.deadline_monotonic,
        )
    if isinstance(error, (EventWriteConflict, PendingRuntimeEventWriteError)):
        return EventBatchCommitOutcome(
            status="none",
            deadline_monotonic=cancelled.deadline_monotonic,
        )
    return EventBatchCommitOutcome(
        status="unknown",
        deadline_monotonic=cancelled.deadline_monotonic,
    )


def _next_publish_sequence(event_log: EventLog) -> int:
    return event_log.next_sequence()


def _merge_event_metadata(
    default_metadata: dict[str, Any], event_metadata: dict[str, Any]
) -> dict[str, Any]:
    metadata = dict(default_metadata)
    for key, value in event_metadata.items():
        if isinstance(metadata.get(key), dict) and isinstance(value, dict):
            nested = dict(metadata[key])
            nested.update(value)
            metadata[key] = nested
        else:
            metadata[key] = value
    return metadata


def _metadata_contains_default(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(
            key in actual and _metadata_contains_default(actual[key], value)
            for key, value in expected.items()
        )
    return actual == expected


def _event_sequence(event: AgentEvent) -> int:
    if event.sequence is None:
        raise ValueError("Committed event is missing canonical sequence")
    return event.sequence


def _same_event_candidate(stored: AgentEvent, candidate: AgentEvent) -> bool:
    return stored.model_dump(mode="json", exclude={"sequence"}) == candidate.model_dump(
        mode="json", exclude={"sequence"}
    )


def _validate_continuation_exposure_subset(source: Any, effective: Any) -> None:
    """Fail closed if a durable continuation reveals anything not in source."""

    source_auth = {
        entry.capability_name: entry for entry in source.authorization_entries
    }
    effective_auth = {
        entry.capability_name: entry for entry in effective.authorization_entries
    }
    if set(source_auth) != set(effective_auth):
        raise ValueError("continuation authorization name set changed")
    rank = {"hidden": 0, "deferred": 1, "direct": 2}
    for name, entry in effective_auth.items():
        original = source_auth[name]
        if (
            entry.descriptor_fingerprint != original.descriptor_fingerprint
            or entry.binding_fingerprint != original.binding_fingerprint
            or rank[entry.disposition] > rank[original.disposition]
        ):
            raise ValueError("continuation authorization widened or changed")

    source_surface = {
        entry.capability_name: entry
        for entry in source.semantic.execution_surface.entries
    }
    for entry in effective.semantic.execution_surface.entries:
        original = source_surface.get(entry.capability_name)
        if original is None or (
            entry.provider_id != original.provider_id
            or entry.descriptor_id != original.descriptor_id
            or entry.descriptor_fingerprint != original.descriptor_fingerprint
            or entry.binding_fingerprint != original.binding_fingerprint
            or entry.binding_contract_id != original.binding_contract_id
            or entry.binding_contract_version != original.binding_contract_version
        ):
            raise ValueError("continuation execution surface is not a source subset")

    for source_projection, effective_projection in (
        (
            source.semantic.catalog_projection,
            effective.semantic.catalog_projection,
        ),
        (
            source.semantic.active_skill_projection,
            effective.semantic.active_skill_projection,
        ),
    ):
        source_entries = {
            entry.projection_entry_id: entry
            for entry in source_projection.visible_source_entries
        }
        for entry in effective_projection.visible_source_entries:
            original = source_entries.get(entry.projection_entry_id)
            if (
                original is None
                or original.content_fingerprint != entry.content_fingerprint
            ):
                raise ValueError("continuation projection contains a new source entry")
        source_fragments = {
            fragment.fragment_id: fragment
            for fragment in source_projection.rendered_fragments
        }
        for fragment in effective_projection.rendered_fragments:
            original = source_fragments.get(fragment.fragment_id)
            if original is None or (
                fragment.fragment_fingerprint != original.fragment_fingerprint
                or fragment.fragment_artifact_id != original.fragment_artifact_id
                or fragment.source_entry_id != original.source_entry_id
                or fragment.source_content_fingerprint
                != original.source_content_fingerprint
            ):
                raise ValueError("continuation projection contains a new fragment")


def _contiguous_interval(
    events: Iterable[AgentEvent],
    *,
    start: int,
    end: int,
) -> tuple[AgentEvent, ...]:
    if end < start:
        return ()
    selected = tuple(
        event for event in events if start <= _event_sequence(event) <= end
    )
    actual = [_event_sequence(event) for event in selected]
    expected = list(range(start, end + 1))
    if actual != expected:
        raise EventReconciliationRequired(
            f"EventLog interval is not contiguous: expected {start}..{end}, got {actual}"
        )
    return selected


def _publication_status(
    enqueue: PublisherEnqueueResult,
) -> Literal["enqueued", "unavailable"]:
    return "unavailable" if enqueue.status == "unavailable" else "enqueued"


async def _await_publication(
    events: tuple[AgentEvent, ...],
    futures: tuple[Future[None], ...],
) -> tuple[EventPublicationError, ...]:
    if not futures:
        return ()
    outcomes = await asyncio.gather(
        *(asyncio.wrap_future(item) for item in futures),
        return_exceptions=True,
    )
    errors: list[EventPublicationError] = []
    for event, outcome in zip(events, outcomes, strict=True):
        if not isinstance(outcome, BaseException):
            continue
        errors.append(
            EventPublicationError(
                event_id=event.id,
                sequence=_event_sequence(event),
                subscriber_id=None,
                error_type=type(outcome).__name__,
                message=_bounded_error(outcome),
            )
        )
    return tuple(errors)


def _bounded_error(exc: BaseException, *, max_chars: int = 500) -> str:
    message = str(exc).replace("\n", " ").strip()
    if len(message) <= max_chars:
        return message
    return message[: max_chars - 1] + "…"
