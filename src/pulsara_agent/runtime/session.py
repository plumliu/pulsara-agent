"""Runtime session ownership for one active Pulsara backend run."""

from __future__ import annotations

from pulsara_agent.event_log.historical_decoder import decode_raw_stored_event_envelope

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import Future
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import Any, Iterable, Literal, cast
from uuid import uuid4

from pulsara_agent.event import (
    AgentEvent,
    CapabilityExposureResolvedEvent,
    ContextCompactionCompletedEvent,
    ContextCompactionFailedEvent,
    ContextCompactionMemoryExtractionRequestedEvent,
    ContextCompactionRequestedEvent,
    ContextCompactionStartedEvent,
    ContextCompiledEvent,
    ContextWindowClosedEvent,
    ContextWindowCompactionCompletedEvent,
    ContextWindowCompactionFailedEvent,
    ContextWindowCompactionStartedEvent,
    ContextWindowOpenedEvent,
    DataBlockSegmentEvent,
    EventContext,
    ExternalExecutionResultEvent,
    McpContinuationDispatchReservedEvent,
    McpCapabilitySnapshotInstalledEvent,
    McpInputRequiredBindingChangedEvent,
    McpInputRequiredExpiredEvent,
    McpInputRequiredInteractionClosedEvent,
    McpInputRequiredResolutionSubmittedEvent,
    McpInputRequiredResumeFailedEvent,
    ModelCallEndEvent,
    ModelCallControlDispositionResolvedEvent,
    ModelCallStartEvent,
    ModelCallTerminalProjectionCommittedEvent,
    PhysicalOperationReservationCreatedEvent,
    RolloutBudgetReservationSettledEvent,
    EventType,
    RunEndEvent,
    RunInteractionResumeBoundaryEvent,
    RunStartEvent,
    RequireExternalExecutionEvent,
    SubagentPhaseReportedEvent,
    SubagentRunCancelledEvent,
    SubagentRunCompletedEvent,
    SubagentRunFailedEvent,
    SubagentRunStartedEvent,
    SubagentRunSuspendedEvent,
    TerminalProcessCompletedEvent,
    TerminalProcessMonitorObservationCommittedEvent,
    TerminalProcessMonitorReceiptAppliedEvent,
    TerminalProcessMonitorRegisteredEvent,
    TerminalProcessMonitorTerminatedEvent,
    TerminalProcessObservationDeliveryDispositionEvent,
    TextBlockSegmentEvent,
    ThinkingBlockSegmentEvent,
    ToolCallArgumentsSegmentEvent,
    ToolResultEndEvent,
    ToolResultDataDeltaEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
    ToolResultTerminalProjectionCommittedEvent,
    ToolExecutionSuspendedEvent,
    MidTurnContextCompactionSkippedEvent,
    ToolResultEvidenceProjectionFailedEvent,
)
from pulsara_agent.event_log import (
    EventBatchConfirmation,
    EventIdConflict,
    EventLog,
    EventLogWriteConflict,
    InMemoryEventLog,
    DEFAULT_EVENT_SCHEMA_REGISTRY,
)
from pulsara_agent.event_log.protocol import EventLogTransactionCompanion
from pulsara_agent.memory.candidates.proposal_sink import MemoryProposalSink
from pulsara_agent.llm.execution import ModelStreamExecutionRegistry
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.primitives.context import (
    ContextStaticInstructionFact,
    context_fingerprint,
)
from pulsara_agent.primitives.stored_event import (
    CanonicalJsonObjectCarrier,
    RawRuntimeProjectionCheckpoint,
    canonical_json_object_carrier,
    runtime_projection_checkpoint_fingerprint,
)
from pulsara_agent.primitives.authority_materialization import (
    LedgerMaterializationConsumerKind,
    LedgerWriteAdmissionClass,
    PhysicalOperationKind,
    PhysicalOperationReservationFact,
    ToolDeltaBurstContractFact,
)
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
from pulsara_agent.runtime.publisher import (
    PublisherEnqueueResult,
    RuntimeEventPublisher,
    RuntimePublishedEvent,
)
from pulsara_agent.runtime.terminal import (
    BorrowedWorkspaceTerminalRuntime,
    OwnedTerminalRuntime,
    TerminalOwnerContext,
    TerminalRuntimeBinding,
    TerminalSessionManager,
)
from pulsara_agent.runtime.terminal_presentation import (
    UiCommittedEventTap,
    UiOperationalActivityStore,
)
from pulsara_agent.primitives.terminal_presentation import LiveCommittedFoldResult


from pulsara_agent.runtime.tool_artifacts import (
    ToolResultArtifactIndex,
    ToolResultArtifactService,
)
from pulsara_agent.runtime.tool_execution import ToolExecutionTerminalRegistry
from pulsara_agent.runtime.publication_maintenance import (
    PublicationTerminalMaintenanceAttempt,
    PublicationTerminalMaintenanceCoordinator,
    PublicationTerminalMaintenanceLease,
    PublicationTerminalMaintenanceOwnerKind,
    validate_publication_latched_run_termination_authority,
)
from pulsara_agent.runtime.projection_checkpoint_maintenance import (
    CHECKPOINT_RECOVERY_HARD_BYTES,
    CHECKPOINT_RECOVERY_HARD_EVENTS,
    CheckpointedCommittedReducerIngress,
    CommittedReducerFoldReceipt,
    PreparedCheckpointedCommittedReducerFold,
    RuntimeProjectionCheckpointAdmissionBlocked,
    RuntimeProjectionCheckpointMaintenanceService,
    RuntimeProjectionRecoveryBoundExceeded,
    build_committed_reducer_fold_receipt,
    read_bounded_runtime_projection_recovery_delta,
)
from pulsara_agent.runtime.committed_reducer_repair import (
    CommittedReducerRepairPlan,
    CommittedReducerRepairReceipt,
    CommittedReducerRepairService,
    OnlineReducerRepairBoundExceeded,
    build_committed_reducer_repair_receipt,
)
from pulsara_agent.runtime.committed_reducer_post_fold import (
    CommittedReducerPostFoldService,
)
from pulsara_agent.runtime.mandatory_audit import RuntimeSessionMandatoryAuditOwner
from pulsara_agent.primitives.runtime_event_vocabulary import (
    RuntimeEventOperationDeadlineBudget,
)
from pulsara_agent.ports.event_write import (
    CommittedEventSettlementReceipt,
    CommittedReducerError,
    EventBatchCommitOutcome,
    EventCommitError,
    EventPublicationAfterCommitError,  # noqa: F401 - historical public import owner
    EventPublicationError,
    EventReconciliationRequired,
    EventWriteCancelled,
    EventWriteResult,
    RuntimeThreadEventBatchSettlementReceipt,
    RuntimeThreadEventSettlementReceipt,
    business_accounting_partition_fingerprint,
    classify_committed_event_settlement,
    event_batch_commit_outcome_from_error as _base_event_batch_commit_outcome,
)
from pulsara_agent.ports.mcp import McpContinuationTransactionAuthority
from pulsara_agent.ports.stored_event import (
    CommittedReducerIngressPort,
    CommittedReducerRebuildPort,
    GroupingIndependentOwnedEventReducerAdapter,
    StoredEventBatchCommitReceipt,
)


RuntimeTransactionCompanion = (
    EventLogTransactionCompanion | McpContinuationTransactionAuthority
)


_ACTIVE_PUBLICATION_MAINTENANCE_LEASE_ID: ContextVar[str | None] = ContextVar(
    "pulsara_active_publication_maintenance_lease_id",
    default=None,
)


@dataclass(frozen=True, slots=True)
class RuntimeThreadRecorder:
    runtime_session: "RuntimeSession"

    def __call__(self, event: AgentEvent) -> RuntimeThreadEventSettlementReceipt:
        return self.runtime_session.settle_event_from_thread(event)


@dataclass(frozen=True, slots=True)
class RuntimeEventSnapshot:
    events: tuple[AgentEvent, ...]
    through_sequence: int


@dataclass(frozen=True, slots=True)
class ProviderInputCloseQuiesceReceipt:
    """Process-local proof that all provider-input close candidates were owned.

    The receipt deliberately proves only producer quiescence.  Reducer repair
    remains a separate Host close barrier and must consume every durable FULL
    before the synchronous RuntimeSession finalizer accepts this receipt.
    """

    runtime_session_id: str
    preparation_outcomes: tuple[tuple[str, str, str | None], ...]
    generation_close_event_ids: tuple[str, ...]


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


def event_batch_commit_outcome_from_error(
    error: BaseException,
) -> EventBatchCommitOutcome | None:
    """Return an outcome already resolved by the writer without new ledger I/O."""

    base = _base_event_batch_commit_outcome(error)
    if base is not None:
        return base
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
    ingress: CommittedReducerIngressPort
    rebuild_owner: CommittedReducerRebuildPort | None = None
    reconciliation_required: bool = False
    last_error: str | None = None
    last_fold_receipt: CommittedReducerFoldReceipt | None = None
    post_fold: Callable[[tuple[AgentEvent, ...]], None] | None = None


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
    dynamic_tool_installations: tuple[object, ...] = ()
    window_compaction_service: Any | None = None
    mcp_supervisor: Any | None = None
    mcp_tool_execution_port: Any | None = None
    context_event_log_locator: Any | None = None
    rollout_account_owner_state_store: Any | None = None
    allow_unbootstrapped_test_events: bool = False
    reopen_deadline_monotonic: float | None = None
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
    authority_materialization_contracts: Any = field(
        init=False,
        repr=False,
    )
    authority_materialization_shadow: Any = field(
        init=False,
        repr=False,
    )
    materialization_account_store: Any = field(
        init=False,
        repr=False,
    )
    materialization_coordinator: Any = field(
        init=False,
        repr=False,
    )
    checkpoint_dispatch_barrier_coordinator: Any = field(
        init=False,
        repr=False,
    )
    _physical_reservation_facts: dict[
        tuple[PhysicalOperationKind, str], PhysicalOperationReservationFact
    ] = field(default_factory=dict, init=False, repr=False)
    _physical_operation_admission_tokens: dict[
        tuple[PhysicalOperationKind, str], Any
    ] = field(default_factory=dict, init=False, repr=False)
    terminal_projection_contracts: Any = field(
        init=False,
        repr=False,
    )
    tool_terminal_projection_state_store: Any = field(
        init=False,
        repr=False,
    )
    tool_terminal_projection_service: Any = field(
        init=False,
        repr=False,
    )
    transcript_projection_document_registry: Any = field(
        init=False,
        repr=False,
    )
    transcript_projection_state_store: Any = field(
        init=False,
        repr=False,
    )
    transcript_projection_materialization_contracts: Any = field(
        init=False,
        repr=False,
    )
    transcript_projection_restore: Any = field(
        init=False,
        repr=False,
    )
    ui_committed_event_tap: UiCommittedEventTap = field(init=False, repr=False)
    ui_operational_activity_store: UiOperationalActivityStore = field(
        init=False, repr=False
    )
    presentation_history_materialization_policy: Any = field(init=False, repr=False)
    presentation_purpose_policy_registry: Any = field(init=False, repr=False)
    presentation_audit_extractor_binding: Any = field(init=False, repr=False)
    presentation_history_checkpoint_owner: Any = field(init=False, repr=False)
    terminal_presentation_foundation_service: Any = field(init=False, repr=False)
    prompt_queue_projection_store: Any = field(init=False, repr=False)
    prompt_queue_artifact_storage: Any = field(init=False, repr=False)
    terminal_command_receipt_storage: Any = field(init=False, repr=False)
    prompt_queue_mutation_service: Any = field(init=False, repr=False)
    prompt_queue_checkpoint_service: Any = field(init=False, repr=False)
    _presentation_restore_range: Any = field(init=False, repr=False, default=None)
    _presentation_restore_fold: Any = field(init=False, repr=False, default=None)
    _transcript_reducer_id: str = field(init=False, repr=False)
    provider_input_generation_store: Any = field(
        init=False,
        repr=False,
    )
    provider_input_generation_coordinator: Any = field(
        init=False,
        repr=False,
    )
    provider_input_preparation_recovery_service: Any = field(
        init=False,
        repr=False,
    )
    _provider_input_close_quiesce_task: (
        asyncio.Task[ProviderInputCloseQuiesceReceipt] | None
    ) = field(
        default=None,
        init=False,
        repr=False,
    )
    _provider_input_close_quiesce_receipt: ProviderInputCloseQuiesceReceipt | None = (
        field(
            default=None,
            init=False,
            repr=False,
        )
    )
    transcript_projection_checkpoint_service: Any = field(
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
    model_call_control_disposition_owner: Any = field(
        init=False,
        repr=False,
    )
    tool_execution_terminal_registry: ToolExecutionTerminalRegistry = field(
        init=False,
        repr=False,
    )
    mcp_input_required_lifecycle_store: Any = field(init=False, repr=False)
    mandatory_runtime_audit_owner: RuntimeSessionMandatoryAuditOwner = field(
        init=False,
        repr=False,
    )
    terminal_monitor_store: Any = field(init=False, repr=False)
    terminal_monitor_coordinator: Any = field(init=False, repr=False)
    terminal_monitor_event_channel: Any = field(init=False, repr=False)
    terminal_notification_store: Any = field(init=False, repr=False)
    terminal_notification_account_coordinator: Any = field(init=False, repr=False)
    runtime_projection_checkpoint_maintenance_service: RuntimeProjectionCheckpointMaintenanceService = field(
        init=False, repr=False
    )
    committed_reducer_repair_service: CommittedReducerRepairService = field(
        init=False, repr=False
    )
    committed_reducer_post_fold_service: CommittedReducerPostFoldService = field(
        init=False, repr=False
    )
    _runtime_open_deadline_monotonic: float = field(init=False, repr=False)
    _terminal_notification_listener: Callable[[tuple[AgentEvent, ...]], None] | None = (
        field(
            default=None,
            init=False,
            repr=False,
        )
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
    _memory_governance_reconciliation_required: bool = field(
        default=False,
        init=False,
        repr=False,
    )
    _publication_reconciliation_required: bool = field(
        default=False,
        init=False,
        repr=False,
    )
    _mandatory_audit_reconciliation_required: bool = field(
        default=False,
        init=False,
        repr=False,
    )
    _publication_latch_generation: int = field(default=0, init=False, repr=False)
    _publication_maintenance_lock: RLock = field(
        default_factory=RLock,
        init=False,
        repr=False,
    )
    _publication_maintenance_coordinator: PublicationTerminalMaintenanceCoordinator = (
        field(init=False, repr=False)
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
    _host_ingress_commit_validator: Callable[[RunStartEvent], None] | None = field(
        default=None, init=False, repr=False
    )
    _host_ingress_commit_guard_factory: (
        Callable[[RunStartEvent], AbstractContextManager[None]] | None
    ) = field(default=None, init=False, repr=False)
    _active_run_monitor_safe_point_provider: (
        Callable[[Any, int], Awaitable[Any]] | None
    ) = field(default=None, init=False, repr=False)
    _active_run_monitor_safe_point_validator: Callable[..., None] | None = field(
        default=None, init=False, repr=False
    )
    _active_run_monitor_safe_point_releaser: Callable[[Any], None] | None = field(
        default=None, init=False, repr=False
    )
    _active_run_monitor_safe_point_commit_guard_factory: (
        Callable[..., AbstractContextManager[None]] | None
    ) = field(default=None, init=False, repr=False)
    _active_run_prompt_steer_safe_point_provider: (
        Callable[[Any, int], Awaitable[Any]] | None
    ) = field(default=None, init=False, repr=False)
    _active_run_prompt_steer_safe_point_validator: Callable[..., None] | None = field(
        default=None, init=False, repr=False
    )
    _active_run_prompt_steer_safe_point_releaser: Callable[[Any], None] | None = field(
        default=None, init=False, repr=False
    )
    _active_run_prompt_steer_commit_guard_factory: (
        Callable[..., AbstractContextManager[None]] | None
    ) = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._runtime_open_deadline_monotonic = (
            monotonic() + 30.0
            if self.reopen_deadline_monotonic is None
            else self.reopen_deadline_monotonic
        )
        if self._runtime_open_deadline_monotonic <= monotonic():
            raise TimeoutError("runtime session open deadline expired before bootstrap")
        self.workspace_root = self.workspace_root.expanduser().resolve()
        if self.allow_unbootstrapped_test_events and not isinstance(
            self.event_log, InMemoryEventLog
        ):
            raise ValueError(
                "unbootstrapped event writes are restricted to the pytest in-memory double"
            )
        bind_event_log_owner = getattr(self.event_log, "bind_runtime_session_id", None)
        if bind_event_log_owner is not None:
            bind_event_log_owner(self.runtime_session_id)
        self.event_log.ensure_runtime_session_owner()
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
        self.committed_reducer_repair_service = CommittedReducerRepairService(
            repair_operation=self._repair_committed_reducer_from_checkpoint
        )
        self.committed_reducer_post_fold_service = CommittedReducerPostFoldService()
        self._publication_maintenance_coordinator = (
            PublicationTerminalMaintenanceCoordinator(
                runtime_session_id=self.runtime_session_id
            )
        )
        self.context_candidate_lifecycle_cache = InMemoryContextLifecycleCache()
        self.context_authority_slice_cache = InMemoryContextAuthoritySliceCache()
        self.tool_result_render_cache = InMemoryToolResultRenderCache()
        self.model_stream_execution_registry = ModelStreamExecutionRegistry()
        from pulsara_agent.llm.control import (
            SessionModelCallControlDispositionOwner,
        )

        self.model_call_control_disposition_owner = (
            SessionModelCallControlDispositionOwner()
        )
        self.tool_execution_terminal_registry = ToolExecutionTerminalRegistry(self)
        self.mandatory_runtime_audit_owner = RuntimeSessionMandatoryAuditOwner(self)
        from pulsara_agent.llm.terminal_projection import (
            build_default_terminal_projection_contract_bundle,
        )

        self.terminal_projection_contracts = (
            build_default_terminal_projection_contract_bundle()
        )
        from pulsara_agent.runtime.authority_materialization import (
            build_default_authority_materialization_contract_bundle,
        )

        self.authority_materialization_contracts = (
            build_default_authority_materialization_contract_bundle()
        )
        from pulsara_agent.runtime.authority_materialization import (
            LedgerMaterializationAccountStore,
            LedgerMaterializationCoordinator,
        )

        durable_account = self.event_log.read_materialization_account_state(
            deadline_monotonic=self._runtime_open_deadline_monotonic
        )
        ledger_usage = self.event_log.read_ledger_usage_snapshot(
            deadline_monotonic=self._runtime_open_deadline_monotonic
        )
        if (
            durable_account is None
            and ledger_usage.through_sequence != 0
            and not self.allow_unbootstrapped_test_events
        ):
            raise ValueError(
                "non-empty ledger lacks the required materialization account; "
                "reset the database for this hard-cut schema"
            )
        if durable_account is not None and (
            durable_account.runtime_session_id != self.runtime_session_id
            or durable_account.ledger_through_sequence != ledger_usage.through_sequence
        ):
            raise ValueError(
                "durable materialization account does not cover the ledger high-water"
            )
        self.materialization_account_store = LedgerMaterializationAccountStore(
            state=durable_account,
            charge_contract=(self.authority_materialization_contracts.charge_contract),
        )
        self.materialization_coordinator = LedgerMaterializationCoordinator(
            runtime_session_id=self.runtime_session_id,
            event_log=self.event_log,
            store=self.materialization_account_store,
            charge_contract=(self.authority_materialization_contracts.charge_contract),
            limits=self.authority_materialization_contracts.limits,
            prepare_event=self.prepare_event_for_write,
        )
        from pulsara_agent.runtime.authority_materialization.dispatch_barrier import (
            CheckpointDispatchBarrierCoordinator,
        )

        self.checkpoint_dispatch_barrier_coordinator = (
            CheckpointDispatchBarrierCoordinator(
                active_barrier=(
                    None
                    if durable_account is None
                    else durable_account.active_checkpoint_barrier
                )
            )
        )
        self.event_write_service.bind_admission_coordinator(
            self.checkpoint_dispatch_barrier_coordinator
        )
        self._restore_active_physical_reservations(durable_account)
        from pulsara_agent.runtime.authority_materialization import (
            build_default_transcript_projection_materialization_contracts,
        )

        self.transcript_projection_materialization_contracts = (
            build_default_transcript_projection_materialization_contracts(
                self.authority_materialization_contracts.limits
            )
        )
        from pulsara_agent.runtime.authority_materialization import (
            restore_transcript_projection,
        )

        transcript_restore = restore_transcript_projection(
            event_log=self.event_log,
            archive=self.archive,
            runtime_session_id=self.runtime_session_id,
            requested_through_sequence=ledger_usage.through_sequence,
            event_domain_binding=(
                self.authority_materialization_contracts.event_domain
            ),
            materialization_contracts=(
                self.transcript_projection_materialization_contracts
            ),
            limits=self.authority_materialization_contracts.limits,
            deadline_monotonic=self._runtime_open_deadline_monotonic,
            allow_seedless_test_bootstrap=(
                self.allow_unbootstrapped_test_events
                and isinstance(self.event_log, InMemoryEventLog)
            ),
        )
        from pulsara_agent.primitives.presentation_history import (
            build_default_history_materialization_policy,
        )
        from pulsara_agent.runtime.terminal_presentation.history_checkpoint import (
            PresentationHistoryProjectionCheckpointOwner,
        )
        from pulsara_agent.runtime.terminal_presentation.policy import (
            PresentationPurposePolicyRegistry,
            build_default_audit_extractor_binding,
            build_default_presentation_purpose_policy_registry,
        )
        from pulsara_agent.runtime.terminal_presentation.restore import (
            restore_transcript_with_presentation_spine,
        )

        self.presentation_history_materialization_policy = (
            build_default_history_materialization_policy()
        )
        self.presentation_purpose_policy_registry = PresentationPurposePolicyRegistry(
            build_default_presentation_purpose_policy_registry(
                transcript_domains=self.authority_materialization_contracts.event_domain
            )
        )
        self.presentation_audit_extractor_binding = (
            build_default_audit_extractor_binding()
        )
        self.presentation_history_checkpoint_owner = (
            PresentationHistoryProjectionCheckpointOwner(
                runtime_session_id=self.runtime_session_id,
                event_log=self.event_log,
                archive=self.archive,
                materialization_policy=(
                    self.presentation_history_materialization_policy
                ),
                purpose_policy=self.presentation_purpose_policy_registry,
                audit_extractor=self.presentation_audit_extractor_binding,
            )
        )
        presentation_genesis = (
            self.presentation_history_checkpoint_owner.ensure_genesis(
                deadline_monotonic=self._runtime_open_deadline_monotonic
            )
        )
        if presentation_genesis.disposition != "full":
            raise ValueError(
                "presentation history genesis could not be durably confirmed"
            )
        presentation_spine = (
            self.presentation_history_checkpoint_owner.read_spine_acceleration(
                deadline_monotonic=self._runtime_open_deadline_monotonic
            )
        )
        presentation_restore = restore_transcript_with_presentation_spine(
            current_restore=transcript_restore,
            acceleration=presentation_spine,
            event_log=self.event_log,
            archive=self.archive,
            runtime_session_id=self.runtime_session_id,
            requested_through_sequence=ledger_usage.through_sequence,
            authority_contracts=self.authority_materialization_contracts,
            materialization_contracts=(
                self.transcript_projection_materialization_contracts
            ),
            deadline_monotonic=self._runtime_open_deadline_monotonic,
            allow_seedless_test_bootstrap=(
                self.allow_unbootstrapped_test_events
                and isinstance(self.event_log, InMemoryEventLog)
            ),
        )
        transcript_restore = presentation_restore.transcript_restore
        self._presentation_restore_range = (
            presentation_restore.presentation_catch_up_range
        )
        self._presentation_restore_fold = (
            presentation_restore.presentation_catch_up_fold
        )
        self.transcript_projection_restore = transcript_restore
        self.transcript_projection_document_registry = (
            transcript_restore.document_registry
        )
        self.transcript_projection_state_store = transcript_restore.state_store
        self.ui_committed_event_tap = UiCommittedEventTap(
            runtime_session_id=self.runtime_session_id
        )
        self.ui_operational_activity_store = UiOperationalActivityStore(
            runtime_session_id=self.runtime_session_id
        )
        self._transcript_reducer_id = f"transcript_projection:{self.runtime_session_id}"
        self.register_committed_reducer(
            reducer_id=self._transcript_reducer_id,
            through_sequence=self.transcript_projection_state_store.through_sequence,
            ingress=self.transcript_projection_state_store,
            rebuild_owner=self.transcript_projection_state_store,
        )
        from pulsara_agent.runtime.terminal_presentation.service import (
            TerminalPresentationFoundationService,
        )

        self.terminal_presentation_foundation_service = (
            TerminalPresentationFoundationService(runtime_session=self)
        )
        self.terminal_presentation_foundation_service.initialize(
            deadline_monotonic=self._runtime_open_deadline_monotonic
        )
        from pulsara_agent.runtime.provider_input import (
            ProviderInputGenerationCoordinator,
            ProviderInputGenerationStore,
            ProviderInputPreparationRecoveryService,
        )

        provider_input_bootstrap = self.event_log.read_raw_events_by_types(
            (
                EventType.CONTEXT_COMPILED.value,
                EventType.PROVIDER_INPUT_GENERATION_STARTED.value,
                EventType.PROVIDER_INPUT_APPEND_COMMITTED.value,
                EventType.PROVIDER_INPUT_EXISTING_PREPARATION_ABANDONED.value,
                EventType.PROVIDER_INPUT_SCOPED_PREPARATION_ABANDONED.value,
                EventType.PROVIDER_INPUT_GENERATION_ROLLOVER_RESOLVED.value,
                EventType.PROVIDER_INPUT_GENERATION_CLOSED.value,
                EventType.MODEL_CALL_START.value,
                EventType.MODEL_CALL_TERMINAL_PROJECTION_COMMITTED.value,
                EventType.MODEL_CALL_END.value,
                EventType.MODEL_CALL_CONTROL_DISPOSITION_RESOLVED.value,
                EventType.RUN_END.value,
            ),
            # A provider-input generation can be the predecessor of a later
            # run/window generation.  Restricting this sparse read to active
            # runs drops the durable rollover basis after restart.
            active_runs_only=False,
            deadline_monotonic=self._runtime_open_deadline_monotonic,
        )
        self.provider_input_generation_store = (
            ProviderInputGenerationStore.from_sparse_bootstrap(
                tuple(
                    decode_raw_stored_event_envelope(
                        event, DEFAULT_EVENT_SCHEMA_REGISTRY
                    )
                    for event in provider_input_bootstrap.events
                ),
                runtime_session_id=self.runtime_session_id,
                through_sequence=provider_input_bootstrap.through_sequence,
            )
        )
        self.provider_input_generation_coordinator = ProviderInputGenerationCoordinator(
            runtime_session=self,
            store=self.provider_input_generation_store,
        )
        self.provider_input_preparation_recovery_service = (
            ProviderInputPreparationRecoveryService(
                runtime_session=self,
                store=self.provider_input_generation_store,
            )
        )
        self.register_committed_reducer(
            reducer_id=f"provider_input_generation:{self.runtime_session_id}",
            through_sequence=self.provider_input_generation_store.through_sequence,
            ingress=GroupingIndependentOwnedEventReducerAdapter(
                apply_owned_events=self.provider_input_generation_store.apply_committed,
                reset_owned_events=lambda: self.provider_input_generation_store.rebuild(
                    ()
                ),
            ),
        )
        from pulsara_agent.runtime.terminal_application.prompt_queue import (
            PromptQueueProjectionStore,
            TerminalPromptQueueMutationService,
        )
        from pulsara_agent.runtime.terminal_application.artifact_hold import (
            build_prompt_queue_artifact_storage,
        )
        from pulsara_agent.runtime.terminal_application.command_receipt import (
            build_terminal_command_receipt_storage,
        )
        from pulsara_agent.runtime.terminal_application.prompt_queue_checkpoint import (
            PromptQueueCheckpointService,
        )

        self.prompt_queue_projection_store = PromptQueueProjectionStore(
            runtime_session_id=self.runtime_session_id
        )
        self.prompt_queue_artifact_storage = build_prompt_queue_artifact_storage(
            self.archive
        )
        self.terminal_command_receipt_storage = build_terminal_command_receipt_storage(
            self.event_log
        )
        self.prompt_queue_checkpoint_service = PromptQueueCheckpointService(
            runtime_session=self,
            store=self.prompt_queue_projection_store,
        )
        self.prompt_queue_checkpoint_service.initialize(
            deadline_monotonic=self._runtime_open_deadline_monotonic
        )
        self.prompt_queue_artifact_storage.release_expired_prepared(
            runtime_session_id=self.runtime_session_id,
            expired_before_utc=datetime.now(UTC).isoformat(),
            maximum_holds=256,
            deadline_monotonic=self._runtime_open_deadline_monotonic,
        )
        self.prompt_queue_mutation_service = TerminalPromptQueueMutationService(
            runtime_session=self,
            store=self.prompt_queue_projection_store,
            artifact_storage=self.prompt_queue_artifact_storage,
        )
        self.register_committed_reducer(
            reducer_id=f"prompt_queue:{self.runtime_session_id}",
            through_sequence=self.prompt_queue_projection_store.through_sequence,
            ingress=GroupingIndependentOwnedEventReducerAdapter(
                apply_owned_events=self.prompt_queue_checkpoint_service.apply_committed,
                reset_owned_events=lambda: self.prompt_queue_projection_store.rebuild(
                    ()
                ),
            ),
        )
        self.prompt_queue_checkpoint_service.start_background_if_possible()
        # Startup recovery may commit a stable checkpoint terminal batch. The
        # publisher must own that newly committed suffix before recovery runs.
        self.publisher = RuntimeEventPublisher(
            runtime_session_id=self.runtime_session_id,
            next_sequence_to_publish=_next_publish_sequence(self.event_log),
        )
        self.publisher.subscribe(self.hook_manager)
        from pulsara_agent.runtime.authority_materialization import (
            TranscriptProjectionCheckpointService,
        )

        self.transcript_projection_checkpoint_service = (
            TranscriptProjectionCheckpointService(runtime_session=self)
        )
        from pulsara_agent.runtime.terminal_projection import (
            ToolTerminalProjectionService,
            ToolTerminalProjectionStateStore,
        )

        tool_projection_bootstrap = self.event_log.read_raw_events_by_types(
            (
                EventType.TOOL_RESULT_START.value,
                EventType.TOOL_RESULT_TEXT_DELTA.value,
                EventType.TOOL_RESULT_DATA_DELTA.value,
                EventType.TOOL_RESULT_END.value,
            ),
            active_runs_only=True,
            deadline_monotonic=self._runtime_open_deadline_monotonic,
        )
        self.tool_terminal_projection_state_store = ToolTerminalProjectionStateStore(
            tuple(
                decode_raw_stored_event_envelope(event, DEFAULT_EVENT_SCHEMA_REGISTRY)
                for event in tool_projection_bootstrap.events
            ),
            through_sequence=tool_projection_bootstrap.through_sequence,
        )
        self.tool_terminal_projection_service = ToolTerminalProjectionService(
            runtime_session=self,
            state_store=self.tool_terminal_projection_state_store,
            contracts=self.terminal_projection_contracts,
        )
        self.register_committed_reducer(
            reducer_id=f"tool_terminal_projection:{self.runtime_session_id}",
            through_sequence=self.tool_terminal_projection_state_store.through_sequence,
            ingress=GroupingIndependentOwnedEventReducerAdapter(
                apply_owned_events=(
                    self.tool_terminal_projection_state_store.apply_committed
                ),
                reset_owned_events=lambda: (
                    self.tool_terminal_projection_state_store.rebuild(())
                ),
            ),
        )
        from pulsara_agent.runtime.mcp.lifecycle import (
            McpInputRequiredLifecycleStore,
        )

        mcp_lifecycle_bootstrap = self.event_log.read_raw_events_by_types(
            (
                EventType.RUN_START.value,
                EventType.RUN_END.value,
                EventType.TOOL_EXECUTION_SUSPENDED.value,
                EventType.MCP_INPUT_REQUIRED_RESOLUTION_SUBMITTED.value,
                EventType.MCP_CONTINUATION_DISPATCH_RESERVED.value,
                EventType.MCP_INPUT_REQUIRED_RESUME_FAILED.value,
                EventType.MCP_INPUT_REQUIRED_EXPIRED.value,
                EventType.MCP_INPUT_REQUIRED_BINDING_CHANGED.value,
                EventType.MCP_INPUT_REQUIRED_INTERACTION_CLOSED.value,
                EventType.TOOL_RESULT_END.value,
            ),
            active_runs_only=True,
            deadline_monotonic=self._runtime_open_deadline_monotonic,
        )
        self.mcp_input_required_lifecycle_store = (
            McpInputRequiredLifecycleStore.from_sparse_bootstrap(
                tuple(
                    decode_raw_stored_event_envelope(
                        event, DEFAULT_EVENT_SCHEMA_REGISTRY
                    )
                    for event in mcp_lifecycle_bootstrap.events
                ),
                runtime_session_id=self.runtime_session_id,
                through_sequence=mcp_lifecycle_bootstrap.through_sequence,
            )
        )
        self.register_committed_reducer(
            reducer_id=f"mcp_input_required_lifecycle:{self.runtime_session_id}",
            through_sequence=self.mcp_input_required_lifecycle_store.through_sequence,
            ingress=GroupingIndependentOwnedEventReducerAdapter(
                apply_owned_events=(
                    self.mcp_input_required_lifecycle_store.apply_committed
                ),
                reset_owned_events=lambda: (
                    self.mcp_input_required_lifecycle_store.rebuild(())
                ),
            ),
        )
        from pulsara_agent.primitives.long_horizon import (
            default_subagent_graph_checkpoint_policy,
        )
        from pulsara_agent.runtime.long_horizon.checkpoint_store import (
            SubagentGraphCheckpointService,
        )
        from pulsara_agent.runtime.long_horizon.reducer_contract import (
            build_default_subagent_graph_reducer_binding,
        )

        graph_checkpoint_policy = default_subagent_graph_checkpoint_policy(
            max_unreclaimable_ledger_events=(
                self.authority_materialization_contracts.limits.max_unreclaimable_ledger_events
            ),
            max_unreclaimable_charged_payload_bytes=(
                self.authority_materialization_contracts.limits.max_unreclaimable_charged_payload_bytes
            ),
        )
        self.subagent_graph_checkpoint_service = SubagentGraphCheckpointService(
            runtime_session=self,
            reducer_binding=build_default_subagent_graph_reducer_binding(),
            policy=graph_checkpoint_policy,
        )
        from pulsara_agent.runtime.long_horizon.store import LongHorizonStateStore
        from pulsara_agent.runtime.long_horizon.rollup import (
            InMemoryObservationRollupContentCache,
            InMemoryPreparedObservationRollupCache,
        )

        self.observation_rollup_content_cache = InMemoryObservationRollupContentCache()
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
        bootstrap_deadline = self._runtime_open_deadline_monotonic
        long_horizon_bootstrap = self.event_log.read_raw_events_by_types(
            long_horizon_types,
            active_runs_only=True,
            deadline_monotonic=bootstrap_deadline,
        )
        self.long_horizon_state_store = LongHorizonStateStore.from_sparse_bootstrap(
            tuple(
                decode_raw_stored_event_envelope(event, DEFAULT_EVENT_SCHEMA_REGISTRY)
                for event in long_horizon_bootstrap.events
            ),
            through_sequence=long_horizon_bootstrap.through_sequence,
        )
        self.register_committed_reducer(
            reducer_id=(f"long_horizon:{self.runtime_session_id}"),
            through_sequence=self.long_horizon_state_store.through_sequence,
            ingress=GroupingIndependentOwnedEventReducerAdapter(
                apply_owned_events=self.long_horizon_state_store.apply_committed,
                reset_owned_events=lambda: self.long_horizon_state_store.rebuild(()),
            ),
        )
        from pulsara_agent.runtime.authority_materialization import (
            AuthorityMaterializationShadowAccount,
        )

        usage_reader = getattr(self.event_log, "read_ledger_usage_snapshot", None)
        if usage_reader is None:
            usage_through = self.long_horizon_state_store.through_sequence
            usage_payload_bytes = 0
        else:
            usage = usage_reader(
                deadline_monotonic=self._runtime_open_deadline_monotonic
            )
            usage_through = usage.through_sequence
            usage_payload_bytes = usage.candidate_payload_bytes
        burst_contracts = tuple(
            item.contract
            for item in self.authority_materialization_contracts.burst_registry.bindings()
        )
        self.authority_materialization_shadow = AuthorityMaterializationShadowAccount(
            through_sequence=usage_through,
            candidate_payload_bytes=usage_payload_bytes,
            limits=self.authority_materialization_contracts.limits,
            charge_contract=self.authority_materialization_contracts.charge_contract,
            fixed_graph_delta_event_bound=(
                graph_checkpoint_policy.checkpoint_max_delta_events
            ),
            fixed_graph_delta_payload_byte_bound=(
                graph_checkpoint_policy.checkpoint_max_delta_bytes
            ),
            resolved_max_burst_events=max(
                item.max_total_reserved_events for item in burst_contracts
            ),
            resolved_max_burst_payload_bytes=max(
                item.max_total_reserved_payload_bytes for item in burst_contracts
            ),
        )
        self.register_committed_reducer(
            reducer_id=(f"authority_materialization_shadow:{self.runtime_session_id}"),
            through_sequence=self.authority_materialization_shadow.through_sequence,
            ingress=GroupingIndependentOwnedEventReducerAdapter(
                apply_owned_events=self.authority_materialization_shadow.apply_committed,
            ),
        )
        self._bind_terminal(self.terminal_binding)
        from pulsara_agent.runtime.terminal.notification import (
            TERMINAL_NOTIFICATION_CHECKPOINT_KIND,
            TERMINAL_NOTIFICATION_CHECKPOINT_SCHEMA_VERSION,
            HostIngressNotificationProjectionStore,
            TerminalNotificationAccountCoordinator,
        )
        from pulsara_agent.runtime.terminal.monitor import (
            TERMINAL_MONITOR_CHECKPOINT_KIND,
            TERMINAL_MONITOR_CHECKPOINT_SCHEMA_VERSION,
            TerminalMonitorCoordinator,
            TerminalMonitorStore,
        )
        from pulsara_agent.runtime.terminal.ui_stream import (
            TerminalMonitorEventChannel,
        )

        self.terminal_notification_store = (
            self._restore_terminal_notification_projection(
                deadline_monotonic=self._runtime_open_deadline_monotonic,
            )
        )
        self.terminal_notification_account_coordinator = (
            TerminalNotificationAccountCoordinator(
                runtime_session_id=self.runtime_session_id,
                store=self.terminal_notification_store,
            )
        )
        self.terminal_monitor_event_channel = TerminalMonitorEventChannel(
            projection_revision=lambda: (
                self.terminal_notification_store.projection_snapshot().source_through_sequence
            ),
            event_resolver=self.event_log.get_by_id,
        )
        self.runtime_projection_checkpoint_maintenance_service = (
            RuntimeProjectionCheckpointMaintenanceService(
                runtime_session_id=self.runtime_session_id,
                event_log=self.event_log,
            )
        )
        notification_reducer_id = f"terminal_notification:{self.runtime_session_id}"
        self.committed_reducer_post_fold_service.register(
            reducer_id=notification_reducer_id,
            callback=self.terminal_notification_account_coordinator.on_committed,
        )
        notification_checkpoint = self.event_log.read_runtime_projection_checkpoint(
            TERMINAL_NOTIFICATION_CHECKPOINT_KIND,
            deadline_monotonic=self._runtime_open_deadline_monotonic,
        )
        self.runtime_projection_checkpoint_maintenance_service.register_projection(
            reducer_id=notification_reducer_id,
            projection_kind=TERMINAL_NOTIFICATION_CHECKPOINT_KIND,
            projection_schema_version=(TERMINAL_NOTIFICATION_CHECKPOINT_SCHEMA_VERSION),
            confirmed_head=notification_checkpoint,
            genesis_state=canonical_json_object_carrier(
                HostIngressNotificationProjectionStore.canonical_checkpoint_genesis_payload(
                    runtime_session_id=self.runtime_session_id
                )
            ),
            current_through_sequence=self.terminal_notification_store.through_sequence,
            current_state=canonical_json_object_carrier(
                self.terminal_notification_store.checkpoint_payload()
            ),
            relevant_event_types=(
                EventType.TERMINAL_NOTIFICATION_RESERVATION_CREATED.value,
                EventType.TERMINAL_NOTIFICATION_RESERVATION_RELEASED.value,
                EventType.TERMINAL_PROCESS_COMPLETED.value,
                EventType.TERMINAL_PROCESS_MONITOR_REGISTERED.value,
                EventType.TERMINAL_PROCESS_MONITOR_OBSERVATION_COMMITTED.value,
                EventType.TERMINAL_PROCESS_MONITOR_RECEIPT_APPLIED.value,
                EventType.TERMINAL_PROCESS_MONITOR_TERMINATED.value,
                EventType.TERMINAL_PROCESS_OBSERVATION_DELIVERY_DISPOSITION.value,
                EventType.TERMINAL_PROCESS_OBSERVATION_DELIVERY_DEFERRED.value,
            ),
            deadline_monotonic=self._runtime_open_deadline_monotonic,
        )
        self.register_committed_reducer(
            reducer_id=notification_reducer_id,
            through_sequence=self.terminal_notification_store.through_sequence,
            ingress=CheckpointedCommittedReducerIngress(
                reducer_id=notification_reducer_id,
                prepare_owned_events=(
                    self.terminal_notification_store.prepare_committed
                ),
                install_prepared_owned_events=(
                    self.terminal_notification_store.install_prepared_committed
                ),
                reset_owned_events=lambda: (
                    self._rebuild_terminal_notification_committed(())
                ),
                checkpoint_relevant=lambda events: any(
                    str(event.type)
                    in {
                        EventType.TERMINAL_NOTIFICATION_RESERVATION_CREATED.value,
                        EventType.TERMINAL_NOTIFICATION_RESERVATION_RELEASED.value,
                        EventType.TERMINAL_PROCESS_COMPLETED.value,
                        EventType.TERMINAL_PROCESS_MONITOR_REGISTERED.value,
                        EventType.TERMINAL_PROCESS_MONITOR_OBSERVATION_COMMITTED.value,
                        EventType.TERMINAL_PROCESS_MONITOR_RECEIPT_APPLIED.value,
                        EventType.TERMINAL_PROCESS_MONITOR_TERMINATED.value,
                        EventType.TERMINAL_PROCESS_OBSERVATION_DELIVERY_DISPOSITION.value,
                        EventType.TERMINAL_PROCESS_OBSERVATION_DELIVERY_DEFERRED.value,
                    }
                    for event in events
                ),
            ),
            post_fold=self._post_terminal_notification_committed_noexcept,
        )

        self.terminal_monitor_store = self._restore_terminal_monitor_projection(
            deadline_monotonic=self._runtime_open_deadline_monotonic,
        )
        self._validate_terminal_projection_checkpoint_join(
            deadline_monotonic=self._runtime_open_deadline_monotonic,
        )
        self.terminal_monitor_coordinator = TerminalMonitorCoordinator(
            runtime_session=self,
            store=self.terminal_monitor_store,
            event_channel=self.terminal_monitor_event_channel,
        )
        monitor_reducer_id = f"terminal_monitor:{self.runtime_session_id}"
        self.committed_reducer_post_fold_service.register(
            reducer_id=monitor_reducer_id,
            callback=self.terminal_monitor_coordinator.on_committed,
        )
        monitor_checkpoint = self.event_log.read_runtime_projection_checkpoint(
            TERMINAL_MONITOR_CHECKPOINT_KIND,
            deadline_monotonic=self._runtime_open_deadline_monotonic,
        )
        self.runtime_projection_checkpoint_maintenance_service.register_projection(
            reducer_id=monitor_reducer_id,
            projection_kind=TERMINAL_MONITOR_CHECKPOINT_KIND,
            projection_schema_version=TERMINAL_MONITOR_CHECKPOINT_SCHEMA_VERSION,
            confirmed_head=monitor_checkpoint,
            genesis_state=canonical_json_object_carrier(
                TerminalMonitorStore.canonical_checkpoint_genesis_payload(
                    runtime_session_id=self.runtime_session_id
                )
            ),
            current_through_sequence=self.terminal_monitor_store.through_sequence,
            current_state=canonical_json_object_carrier(
                self.terminal_monitor_store.checkpoint_payload()
            ),
            relevant_event_types=(
                EventType.TERMINAL_PROCESS_MONITOR_REGISTERED.value,
                EventType.TERMINAL_PROCESS_MONITOR_OBSERVATION_COMMITTED.value,
                EventType.TERMINAL_PROCESS_MONITOR_RECEIPT_APPLIED.value,
                EventType.TERMINAL_PROCESS_MONITOR_TERMINATED.value,
                EventType.TERMINAL_PROCESS_OBSERVATION_DELIVERY_DISPOSITION.value,
            ),
            deadline_monotonic=self._runtime_open_deadline_monotonic,
        )
        self.register_committed_reducer(
            reducer_id=monitor_reducer_id,
            through_sequence=self.terminal_monitor_store.through_sequence,
            ingress=CheckpointedCommittedReducerIngress(
                reducer_id=monitor_reducer_id,
                prepare_owned_events=(self.terminal_monitor_store.prepare_committed),
                install_prepared_owned_events=(
                    self.terminal_monitor_store.install_prepared_committed
                ),
                reset_owned_events=lambda: self._rebuild_terminal_monitor_committed(()),
                checkpoint_relevant=lambda events: any(
                    str(event.type)
                    in {
                        EventType.TERMINAL_PROCESS_MONITOR_REGISTERED.value,
                        EventType.TERMINAL_PROCESS_MONITOR_OBSERVATION_COMMITTED.value,
                        EventType.TERMINAL_PROCESS_MONITOR_RECEIPT_APPLIED.value,
                        EventType.TERMINAL_PROCESS_MONITOR_TERMINATED.value,
                        EventType.TERMINAL_PROCESS_OBSERVATION_DELIVERY_DISPOSITION.value,
                    }
                    for event in events
                ),
            ),
            post_fold=self._post_terminal_monitor_committed_noexcept,
        )
        self.materialization_coordinator.bind_pre_commit_admission(
            self._assert_runtime_projection_physical_batch_admission
        )
        self.terminal_monitor_coordinator.on_committed(
            tuple(
                record.registration_event
                for record in self.terminal_monitor_store.snapshots()
                if record.core_state.lifecycle_state
                not in {"terminated", "reconciliation_required"}
            )
        )
        self.provider_input_preparation_recovery_service.recover_incomplete_preparations_sync(
            deadline_monotonic=self._runtime_open_deadline_monotonic
        )
        self.provider_input_generation_coordinator.close_owned_attempts_after_recovery()

        # Keep the imported identities live for architecture guards and make it
        # impossible for the two checkpoint kinds to silently collapse.
        if TERMINAL_NOTIFICATION_CHECKPOINT_KIND == TERMINAL_MONITOR_CHECKPOINT_KIND:
            raise RuntimeError("terminal reducer checkpoint identities collide")

    @property
    def runtime_open_deadline_monotonic(self) -> float:
        """Return the immutable deadline shared by all open/recovery owners."""

        return self._runtime_open_deadline_monotonic

    def _validate_terminal_projection_checkpoint_join(
        self,
        *,
        deadline_monotonic: float,
    ) -> None:
        """Join the independently checkpointed monitor and notification cores."""

        if monotonic() >= deadline_monotonic:
            raise TimeoutError(
                "runtime session open deadline expired before terminal projection join"
            )

        from pulsara_agent.llm.terminal_projection import stable_event_identity
        from pulsara_agent.runtime.context_input.event_slice import (
            event_reference_from_stored,
        )

        projection = self.terminal_notification_store.projection_snapshot()
        notification_heads = {
            monitor.monitor_id: monitor
            for process in projection.process_heads
            for monitor in process.monitor_heads
        }
        monitor_records = {
            item.registration_event.registration_semantic.monitor_id: item
            for item in self.terminal_monitor_store.snapshots()
        }
        if set(notification_heads) != set(monitor_records):
            raise ValueError(
                "terminal projection checkpoints disagree on active monitor heads"
            )
        for monitor_id, record in monitor_records.items():
            head = notification_heads[monitor_id]
            core = record.core_state
            pending = record.pending_observation_event
            if (
                stable_event_identity(
                    record.registration_event,
                    runtime_session_id=self.runtime_session_id,
                )
                != head.registration_event_identity
                or core.core_state_fingerprint != head.monitor_core_state_fingerprint
                or core.last_committed_observation_ordinal
                != head.last_committed_observation_ordinal
                or core.last_observation_cursor.cursor_fingerprint
                != head.last_observation_cursor_fingerprint
                or core.last_consumed_cursor.cursor_fingerprint
                != head.last_consumed_cursor_fingerprint
                or (
                    None
                    if pending is None
                    else event_reference_from_stored(
                        pending,
                        runtime_session_id=self.runtime_session_id,
                    )
                )
                != head.pending_observation_event_reference
            ):
                raise ValueError(
                    "terminal projection checkpoints disagree on monitor core state"
                )

    def _restore_terminal_notification_projection(
        self,
        *,
        deadline_monotonic: float,
    ):
        from pulsara_agent.runtime.terminal.notification import (
            HostIngressNotificationProjectionStore,
            TERMINAL_NOTIFICATION_CHECKPOINT_KIND,
            TERMINAL_NOTIFICATION_CHECKPOINT_SCHEMA_VERSION,
        )

        deadline = deadline_monotonic
        checkpoint = self.event_log.read_runtime_projection_checkpoint(
            TERMINAL_NOTIFICATION_CHECKPOINT_KIND,
            deadline_monotonic=deadline,
        )
        if checkpoint is None:
            store = HostIngressNotificationProjectionStore(
                runtime_session_id=self.runtime_session_id
            )
            minimum_sequence = 1
        else:
            self._validate_runtime_projection_checkpoint(
                checkpoint,
                expected_kind=TERMINAL_NOTIFICATION_CHECKPOINT_KIND,
                expected_schema_version=(
                    TERMINAL_NOTIFICATION_CHECKPOINT_SCHEMA_VERSION
                ),
                deadline_monotonic=deadline,
            )
            if checkpoint.validation_base_through_sequence == 0:
                canonical_genesis = HostIngressNotificationProjectionStore.canonical_checkpoint_genesis_payload(
                    runtime_session_id=self.runtime_session_id
                )
                if not self._runtime_projection_payloads_equal(
                    checkpoint.validation_base_state_payload,
                    canonical_genesis,
                ):
                    raise ValueError(
                        "terminal notification checkpoint genesis is untrusted"
                    )
            validation_store = (
                HostIngressNotificationProjectionStore.from_checkpoint_payload(
                    checkpoint.validation_base_state_payload,
                    runtime_session_id=self.runtime_session_id,
                )
            )
            validation_events, validation_through, _, _ = (
                self._read_terminal_projection_delta(
                    event_types=(
                        EventType.TERMINAL_NOTIFICATION_RESERVATION_CREATED.value,
                        EventType.TERMINAL_NOTIFICATION_RESERVATION_RELEASED.value,
                        EventType.TERMINAL_PROCESS_COMPLETED.value,
                        EventType.TERMINAL_PROCESS_MONITOR_REGISTERED.value,
                        EventType.TERMINAL_PROCESS_MONITOR_OBSERVATION_COMMITTED.value,
                        EventType.TERMINAL_PROCESS_MONITOR_RECEIPT_APPLIED.value,
                        EventType.TERMINAL_PROCESS_MONITOR_TERMINATED.value,
                        EventType.TERMINAL_PROCESS_OBSERVATION_DELIVERY_DISPOSITION.value,
                        EventType.TERMINAL_PROCESS_OBSERVATION_DELIVERY_DEFERRED.value,
                    ),
                    minimum_sequence=(checkpoint.validation_base_through_sequence + 1),
                    through_sequence=checkpoint.through_sequence,
                    deadline_monotonic=deadline,
                )
                if checkpoint.validation_base_through_sequence
                < checkpoint.through_sequence
                else ((), checkpoint.through_sequence, 0, 0)
            )
            validation_store.apply_committed(validation_events)
            validation_store.through_sequence = max(
                validation_store.through_sequence,
                validation_through,
            )
            if not self._runtime_projection_payloads_equal(
                validation_store.checkpoint_payload(),
                checkpoint.state,
            ):
                raise ValueError(
                    "terminal notification checkpoint reducer transition is untrusted"
                )
            store = HostIngressNotificationProjectionStore.from_checkpoint_payload(
                checkpoint.state_payload,
                runtime_session_id=self.runtime_session_id,
            )
            minimum_sequence = checkpoint.through_sequence + 1
        events, through_sequence, _, _ = self._read_terminal_projection_delta(
            event_types=(
                EventType.TERMINAL_NOTIFICATION_RESERVATION_CREATED.value,
                EventType.TERMINAL_NOTIFICATION_RESERVATION_RELEASED.value,
                EventType.TERMINAL_PROCESS_COMPLETED.value,
                EventType.TERMINAL_PROCESS_MONITOR_REGISTERED.value,
                EventType.TERMINAL_PROCESS_MONITOR_OBSERVATION_COMMITTED.value,
                EventType.TERMINAL_PROCESS_MONITOR_RECEIPT_APPLIED.value,
                EventType.TERMINAL_PROCESS_MONITOR_TERMINATED.value,
                EventType.TERMINAL_PROCESS_OBSERVATION_DELIVERY_DISPOSITION.value,
                EventType.TERMINAL_PROCESS_OBSERVATION_DELIVERY_DEFERRED.value,
            ),
            minimum_sequence=minimum_sequence,
            deadline_monotonic=deadline,
        )
        store.apply_committed(events)
        store.through_sequence = max(store.through_sequence, through_sequence)
        return store

    def _restore_terminal_monitor_projection(
        self,
        *,
        deadline_monotonic: float,
    ):
        from pulsara_agent.runtime.terminal.monitor import (
            TERMINAL_MONITOR_CHECKPOINT_KIND,
            TERMINAL_MONITOR_CHECKPOINT_SCHEMA_VERSION,
            TerminalMonitorStore,
        )

        deadline = deadline_monotonic
        checkpoint = self.event_log.read_runtime_projection_checkpoint(
            TERMINAL_MONITOR_CHECKPOINT_KIND,
            deadline_monotonic=deadline,
        )
        if checkpoint is None:
            store = TerminalMonitorStore(runtime_session_id=self.runtime_session_id)
            minimum_sequence = 1
        else:
            self._validate_runtime_projection_checkpoint(
                checkpoint,
                expected_kind=TERMINAL_MONITOR_CHECKPOINT_KIND,
                expected_schema_version=TERMINAL_MONITOR_CHECKPOINT_SCHEMA_VERSION,
                deadline_monotonic=deadline,
            )
            if checkpoint.validation_base_through_sequence == 0:
                canonical_genesis = (
                    TerminalMonitorStore.canonical_checkpoint_genesis_payload(
                        runtime_session_id=self.runtime_session_id
                    )
                )
                if not self._runtime_projection_payloads_equal(
                    checkpoint.validation_base_state_payload,
                    canonical_genesis,
                ):
                    raise ValueError("terminal monitor checkpoint genesis is untrusted")
            validation_store = TerminalMonitorStore.from_checkpoint_payload(
                checkpoint.validation_base_state_payload,
                runtime_session_id=self.runtime_session_id,
            )
            validation_events, validation_through, _, _ = (
                self._read_terminal_projection_delta(
                    event_types=(
                        EventType.TERMINAL_PROCESS_MONITOR_REGISTERED.value,
                        EventType.TERMINAL_PROCESS_MONITOR_OBSERVATION_COMMITTED.value,
                        EventType.TERMINAL_PROCESS_MONITOR_RECEIPT_APPLIED.value,
                        EventType.TERMINAL_PROCESS_MONITOR_TERMINATED.value,
                        EventType.TERMINAL_PROCESS_OBSERVATION_DELIVERY_DISPOSITION.value,
                    ),
                    minimum_sequence=(checkpoint.validation_base_through_sequence + 1),
                    through_sequence=checkpoint.through_sequence,
                    deadline_monotonic=deadline,
                )
                if checkpoint.validation_base_through_sequence
                < checkpoint.through_sequence
                else ((), checkpoint.through_sequence, 0, 0)
            )
            validation_store.apply_committed(validation_events)
            validation_store.through_sequence = max(
                validation_store.through_sequence,
                validation_through,
            )
            if not self._runtime_projection_payloads_equal(
                validation_store.checkpoint_payload(),
                checkpoint.state,
            ):
                raise ValueError(
                    "terminal monitor checkpoint reducer transition is untrusted"
                )
            store = TerminalMonitorStore.from_checkpoint_payload(
                checkpoint.state_payload,
                runtime_session_id=self.runtime_session_id,
            )
            minimum_sequence = checkpoint.through_sequence + 1
        events, through_sequence, _, _ = self._read_terminal_projection_delta(
            event_types=(
                EventType.TERMINAL_PROCESS_MONITOR_REGISTERED.value,
                EventType.TERMINAL_PROCESS_MONITOR_OBSERVATION_COMMITTED.value,
                EventType.TERMINAL_PROCESS_MONITOR_RECEIPT_APPLIED.value,
                EventType.TERMINAL_PROCESS_MONITOR_TERMINATED.value,
                EventType.TERMINAL_PROCESS_OBSERVATION_DELIVERY_DISPOSITION.value,
            ),
            minimum_sequence=minimum_sequence,
            deadline_monotonic=deadline,
        )
        store.apply_committed(events)
        store.through_sequence = max(store.through_sequence, through_sequence)
        return store

    def _read_terminal_projection_delta(
        self,
        *,
        event_types: tuple[str, ...],
        minimum_sequence: int,
        deadline_monotonic: float,
        through_sequence: int | None = None,
    ) -> tuple[tuple[AgentEvent, ...], int, int, int]:
        try:
            snapshot = self.event_log.read_raw_events_by_types(
                event_types,
                minimum_sequence=minimum_sequence,
                through_sequence=through_sequence,
                max_events=CHECKPOINT_RECOVERY_HARD_EVENTS,
                max_payload_bytes=CHECKPOINT_RECOVERY_HARD_BYTES,
                deadline_monotonic=deadline_monotonic,
            )
        except ValueError as exc:
            message = str(exc).lower()
            if "bound" in message or "exceed" in message:
                raise OnlineReducerRepairBoundExceeded(
                    "terminal reducer sparse delta exceeds its online bound"
                ) from exc
            raise
        authority_envelopes = {item.event_id: item for item in snapshot.events}
        selected = [
            decode_raw_stored_event_envelope(item, DEFAULT_EVENT_SCHEMA_REGISTRY)
            for item in snapshot.events
        ]
        tool_result_ids = {
            event.tool_result_end_event_identity.event_id
            for event in selected
            if isinstance(event, TerminalProcessMonitorReceiptAppliedEvent)
        }
        tool_result_ids.update(
            event.tool_result_end_event_identity.event_id
            for event in selected
            if isinstance(event, TerminalProcessObservationDeliveryDispositionEvent)
            and event.tool_result_end_event_identity is not None
        )
        if tool_result_ids:
            if (
                len(authority_envelopes) + len(tool_result_ids)
                > CHECKPOINT_RECOVERY_HARD_EVENTS
            ):
                raise OnlineReducerRepairBoundExceeded(
                    "terminal reducer authority set exceeds its online event bound"
                )
            exact = self.event_log.read_raw_events_by_id(
                tuple(sorted(tool_result_ids)),
                deadline_monotonic=deadline_monotonic,
            )
            if len(exact) != len(tool_result_ids):
                raise ValueError(
                    "terminal projection delta lacks exact ToolResult authority"
                )
            for item in exact:
                previous = authority_envelopes.get(item.event_id)
                if previous is not None and previous != item:
                    raise ValueError(
                        "terminal projection authority identity is ambiguous"
                    )
                authority_envelopes[item.event_id] = item
            if (
                sum(
                    len(item.canonical_payload_bytes)
                    for item in authority_envelopes.values()
                )
                > CHECKPOINT_RECOVERY_HARD_BYTES
            ):
                raise OnlineReducerRepairBoundExceeded(
                    "terminal reducer authority set exceeds its online byte bound"
                )
            selected.extend(
                decode_raw_stored_event_envelope(item, DEFAULT_EVENT_SCHEMA_REGISTRY)
                for item in exact
            )
        by_id = {event.id: event for event in selected}
        ordered = tuple(
            sorted(by_id.values(), key=lambda event: (event.sequence or 0, event.id))
        )
        return (
            ordered,
            snapshot.through_sequence,
            len(authority_envelopes),
            sum(
                len(item.canonical_payload_bytes)
                for item in authority_envelopes.values()
            ),
        )

    def _validate_runtime_projection_checkpoint(
        self,
        checkpoint: RawRuntimeProjectionCheckpoint,
        *,
        expected_kind: str,
        expected_schema_version: str,
        deadline_monotonic: float,
    ) -> None:
        expected = runtime_projection_checkpoint_fingerprint(
            projection_kind=checkpoint.projection_kind,
            through_sequence=checkpoint.through_sequence,
            projection_schema_version=checkpoint.projection_schema_version,
            ledger_prefix=checkpoint.ledger_prefix,
            validation_base_through_sequence=(
                checkpoint.validation_base_through_sequence
            ),
            validation_base_state=checkpoint.validation_base_state,
            state=checkpoint.state,
        )
        committed_prefix = self.event_log.read_raw_ledger_prefix(
            through_sequence=checkpoint.through_sequence,
            deadline_monotonic=deadline_monotonic,
        )
        if (
            checkpoint.projection_kind != expected_kind
            or checkpoint.projection_schema_version != expected_schema_version
            or checkpoint.ledger_prefix != committed_prefix
            or checkpoint.payload_fingerprint != expected
        ):
            raise ValueError("terminal runtime projection checkpoint is untrusted")

    @staticmethod
    def _runtime_projection_payloads_equal(
        left: dict[str, object] | CanonicalJsonObjectCarrier,
        right: dict[str, object] | CanonicalJsonObjectCarrier,
    ) -> bool:
        left_carrier = (
            left
            if isinstance(left, CanonicalJsonObjectCarrier)
            else canonical_json_object_carrier(left)
        )
        right_carrier = (
            right
            if isinstance(right, CanonicalJsonObjectCarrier)
            else canonical_json_object_carrier(right)
        )
        return left_carrier == right_carrier

    def _restore_active_physical_reservations(self, durable_account: Any) -> None:
        self._physical_reservation_facts = {}
        self._physical_operation_admission_tokens = {}
        if durable_account is None or not durable_account.active_reservations:
            return
        event_ids = tuple(
            item.latest_reservation_event_id
            for item in durable_account.active_reservations
        )
        raw_events = self.event_log.read_raw_events_by_id(
            event_ids,
            deadline_monotonic=self._runtime_open_deadline_monotonic,
        )
        if len(raw_events) != len(event_ids):
            raise ValueError(
                "active physical reservation is missing its durable creation fact"
            )
        by_id = {
            raw.event_id: decode_raw_stored_event_envelope(
                raw, DEFAULT_EVENT_SCHEMA_REGISTRY
            )
            for raw in raw_events
        }
        for active in durable_account.active_reservations:
            event = by_id.get(active.latest_reservation_event_id)
            if not isinstance(event, PhysicalOperationReservationCreatedEvent):
                raise ValueError(
                    "active physical reservation reference is not a creation event"
                )
            reservation = event.reservation
            if (
                reservation.reservation_id != active.reservation_id
                or reservation.reservation_fingerprint != active.reservation_fingerprint
                or reservation.owner_kind != active.owner_kind
                or reservation.owner_id != active.owner_id
            ):
                raise ValueError("active physical reservation durable identity drifted")
            key = (reservation.owner_kind, reservation.owner_id)
            if key in self._physical_reservation_facts:
                raise ValueError("physical reservation owner identity is ambiguous")
            self._physical_reservation_facts[key] = reservation
            if reservation.owner_kind is not PhysicalOperationKind.CHECKPOINT_COMMIT:
                self._physical_operation_admission_tokens[key] = (
                    self.checkpoint_dispatch_barrier_coordinator.restore_operation_admission(
                        operation_owner_id=_physical_operation_admission_owner_id(key)
                    )
                )

    def _post_terminal_monitor_committed_noexcept(
        self, events: tuple[AgentEvent, ...]
    ) -> None:
        self.committed_reducer_post_fold_service.handoff(
            reducer_id=f"terminal_monitor:{self.runtime_session_id}",
            events=events,
        )

    def _post_terminal_notification_committed_noexcept(
        self, events: tuple[AgentEvent, ...]
    ) -> None:
        self.committed_reducer_post_fold_service.handoff(
            reducer_id=f"terminal_notification:{self.runtime_session_id}",
            events=events,
        )
        try:
            self.terminal_monitor_event_channel.publish_committed(events)
        except BaseException:
            # UI replay is operational and may drop independently.  It is not
            # a process-control handoff and therefore is deliberately not
            # retried through the semantic/post-fold owner.
            pass
        listener = self._terminal_notification_listener
        if listener is not None:
            try:
                listener(events)
            except BaseException:
                # The Host listener observes already-durable authority.  Its
                # own ingress/recovery path must not poison this fold.
                pass

    def _rebuild_terminal_notification_committed(
        self, events: tuple[AgentEvent, ...]
    ) -> None:
        self.terminal_notification_store.rebuild(events)

    def _rebuild_terminal_monitor_committed(
        self, events: tuple[AgentEvent, ...]
    ) -> None:
        self.terminal_monitor_store.rebuild(events)

    def _adopt_unbootstrapped_in_memory_account_for_test(
        self,
        *,
        incoming_run_id: str,
    ) -> None:
        """Bridge legacy pytest fixtures without weakening production genesis."""

        if self.materialization_account_store.snapshot() is not None:
            return
        if not (
            self.allow_unbootstrapped_test_events
            and isinstance(self.event_log, InMemoryEventLog)
        ):
            raise ValueError("materialization account adoption is pytest-only")
        from pulsara_agent.event_log.transcript_prefix import (
            EMPTY_LEDGER_CONTINUITY_ACCUMULATOR,
        )
        from pulsara_agent.primitives.authority_materialization import (
            LedgerMaterializationConsumerHorizonFact,
            LedgerMaterializationConsumerKind,
        )
        from pulsara_agent.primitives.frozen import build_frozen_fact
        from pulsara_agent.runtime.authority_materialization import (
            build_account_state,
            build_generation,
            canonical_empty_generation,
            deterministic_ledger_charge,
        )

        existing = tuple(self.event_log.iter())
        through = len(existing)
        charge = deterministic_ledger_charge(
            existing,
            contract=self.authority_materialization_contracts.charge_contract,
        )
        prefix = self.event_log.read_transcript_domain_delta(
            after_sequence=through,
            through_sequence=through,
            max_events=1,
            max_payload_bytes=1,
            registry_contract_fingerprint=(
                self.authority_materialization_contracts.event_domain.contract.registry_contract_fingerprint
            ),
        ).after
        graph = build_frozen_fact(
            LedgerMaterializationConsumerHorizonFact,
            schema_version="ledger_materialization_consumer_horizon.v1",
            runtime_session_id=self.runtime_session_id,
            consumer_kind=LedgerMaterializationConsumerKind.SUBAGENT_GRAPH,
            consumer_id=f"subagent_graph:{self.runtime_session_id}",
            business_run_id=None,
            business_window_id=None,
            business_window_generation=None,
            through_sequence=0,
            ledger_event_count_through=0,
            ledger_charged_payload_bytes_through=0,
            ledger_continuity_accumulator=EMPTY_LEDGER_CONTINUITY_ACCUMULATOR,
            consumer_contract_fingerprint=context_fingerprint(
                "ledger-materialization-consumer-contract:v1",
                {
                    "kind": LedgerMaterializationConsumerKind.SUBAGENT_GRAPH.value,
                    "consumer_id": f"subagent_graph:{self.runtime_session_id}",
                },
            ),
        )
        transcript = build_frozen_fact(
            LedgerMaterializationConsumerHorizonFact,
            schema_version="ledger_materialization_consumer_horizon.v1",
            runtime_session_id=self.runtime_session_id,
            consumer_kind=LedgerMaterializationConsumerKind.TRANSCRIPT_WINDOW,
            consumer_id=f"transcript:test-bootstrap:{incoming_run_id}",
            business_run_id=incoming_run_id,
            business_window_id="test-bootstrap",
            business_window_generation=0,
            through_sequence=through,
            ledger_event_count_through=through,
            ledger_charged_payload_bytes_through=charge.charged_payload_bytes,
            ledger_continuity_accumulator=prefix.ledger_continuity_accumulator,
            consumer_contract_fingerprint=context_fingerprint(
                "ledger-materialization-consumer-contract:v1",
                {
                    "kind": LedgerMaterializationConsumerKind.TRANSCRIPT_WINDOW.value,
                    "consumer_id": f"transcript:test-bootstrap:{incoming_run_id}",
                },
            ),
        )
        empty_generation = canonical_empty_generation(
            runtime_session_id=self.runtime_session_id,
            charge_contract_fingerprint=(
                self.authority_materialization_contracts.charge_contract.contract_fingerprint
            ),
        )
        generation = build_generation(
            source=empty_generation,
            consumer_horizons=(graph, transcript),
            consumer_horizon_revision=1,
        )
        adopted = build_account_state(
            runtime_session_id=self.runtime_session_id,
            generation=generation,
            ledger_through_sequence=through,
            ledger_charged_payload_bytes_through=charge.charged_payload_bytes,
            active_reservations=(),
            active_checkpoint_barrier=None,
            latest_transition_event_ids=(),
            reconciliation_required=False,
            reconciliation_reason_code=None,
        )
        self.event_log.adopt_materialization_account_state_for_test(adopted)
        self.materialization_account_store.install_confirmed_state(adopted)

    def physical_reservation_for_owner(
        self,
        *,
        operation_kind: PhysicalOperationKind,
        owner_id: str,
    ) -> PhysicalOperationReservationFact | None:
        return self._physical_reservation_facts.get((operation_kind, owner_id))

    def resolve_run_rollout_binding(self, *, run_id: str):
        from pulsara_agent.runtime.long_horizon.accounting import (
            resolve_run_rollout_binding,
        )

        return resolve_run_rollout_binding(self, run_id=run_id)

    @staticmethod
    def plan_root_model_admission(*, account, state, quote, purpose):
        from pulsara_agent.runtime.long_horizon.coordinator import (
            plan_root_model_admission,
        )

        return plan_root_model_admission(
            account=account,
            state=state,
            quote=quote,
            purpose=purpose,
        )

    @staticmethod
    def build_one_shot_generation_close_event(*, bundle, event_context):
        from pulsara_agent.runtime.provider_input.planner import (
            build_one_shot_generation_close_event,
        )

        return build_one_shot_generation_close_event(
            bundle=bundle,
            event_context=event_context,
        )

    def physical_operation_admission_owner_id(
        self,
        *,
        operation_kind: PhysicalOperationKind,
        owner_id: str,
    ) -> str:
        """Return the exact process-local gate owner for a durable reservation."""

        return _physical_operation_admission_owner_id((operation_kind, owner_id))

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

    def bind_host_ingress_commit_validator(
        self,
        validator: Callable[[RunStartEvent], None] | None,
    ) -> None:
        with self.write_coordinator.lock:
            self._host_ingress_commit_validator = validator

    def bind_host_ingress_commit_guard(
        self,
        guard_factory: (Callable[[RunStartEvent], AbstractContextManager[None]] | None),
    ) -> None:
        with self.write_coordinator.lock:
            self._host_ingress_commit_guard_factory = guard_factory

    @contextmanager
    def _host_ingress_commit_guard(
        self,
        events: Sequence[AgentEvent],
    ):
        starts = tuple(
            item
            for item in events
            if isinstance(item, RunStartEvent) and item.run_entry_kind.value == "host"
        )
        if len(starts) > 1:
            raise ValueError(
                "one physical batch cannot commit multiple Host RunStart events"
            )
        factory = self._host_ingress_commit_guard_factory
        if not starts or factory is None:
            yield
            return
        with factory(starts[0]):
            yield

    def bind_terminal_notification_listener(
        self,
        listener: Callable[[tuple[AgentEvent, ...]], None] | None,
    ) -> None:
        with self.write_coordinator.lock:
            self._terminal_notification_listener = listener

    def bind_active_run_monitor_safe_point(
        self,
        *,
        provider: Callable[[Any, int], Awaitable[Any]] | None,
        validator: Callable[..., None] | None,
        releaser: Callable[[Any], None] | None,
        commit_guard_factory: (
            Callable[..., AbstractContextManager[None]] | None
        ) = None,
    ) -> None:
        with self.write_coordinator.lock:
            self._active_run_monitor_safe_point_provider = provider
            self._active_run_monitor_safe_point_validator = validator
            self._active_run_monitor_safe_point_releaser = releaser
            self._active_run_monitor_safe_point_commit_guard_factory = (
                commit_guard_factory
            )

    @contextmanager
    def active_run_monitor_safe_point_commit_guard(
        self,
        *,
        start_event: ModelCallStartEvent,
        candidate_events: tuple[AgentEvent, ...],
        guard: Any,
    ):
        factory = self._active_run_monitor_safe_point_commit_guard_factory
        if guard is None or factory is None:
            yield
            return
        with factory(
            start_event=start_event,
            candidate_events=candidate_events,
            guard=guard,
            run_id=start_event.run_id,
        ):
            yield

    async def borrow_active_run_monitor_safe_point(
        self,
        *,
        run_id: str,
        next_model_call_index: int,
    ) -> Any:
        provider = self._active_run_monitor_safe_point_provider
        if provider is None:
            return None
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("active-run safe point requires a typed run identity")
        return await provider(run_id, next_model_call_index)

    def release_active_run_monitor_safe_point(self, lease: Any) -> None:
        releaser = self._active_run_monitor_safe_point_releaser
        if releaser is not None:
            releaser(lease)

    def validate_active_run_monitor_safe_point(
        self,
        *,
        start_event: ModelCallStartEvent,
        candidate_events: tuple[AgentEvent, ...],
        guard: Any,
    ) -> None:
        validator = self._active_run_monitor_safe_point_validator
        if validator is None:
            raise ValueError("active-run monitor safe-point owner is unavailable")
        validator(
            start_event=start_event,
            candidate_events=candidate_events,
            guard=guard,
            run_id=start_event.run_id,
        )

    def bind_active_run_prompt_steer_safe_point(
        self,
        *,
        provider: Callable[[Any, int], Awaitable[Any]] | None,
        validator: Callable[..., None] | None,
        releaser: Callable[[Any], None] | None,
        commit_guard_factory: Callable[..., AbstractContextManager[None]] | None,
    ) -> None:
        with self.write_coordinator.lock:
            self._active_run_prompt_steer_safe_point_provider = provider
            self._active_run_prompt_steer_safe_point_validator = validator
            self._active_run_prompt_steer_safe_point_releaser = releaser
            self._active_run_prompt_steer_commit_guard_factory = commit_guard_factory

    async def borrow_active_run_prompt_steer_safe_point(
        self,
        *,
        run_id: str,
        next_model_call_index: int,
    ) -> Any:
        provider = self._active_run_prompt_steer_safe_point_provider
        if provider is None:
            return None
        return await provider(run_id, next_model_call_index)

    def release_active_run_prompt_steer_safe_point(self, lease: Any) -> None:
        releaser = self._active_run_prompt_steer_safe_point_releaser
        if releaser is not None:
            releaser(lease)

    @contextmanager
    def active_run_prompt_steer_commit_guard(
        self,
        *,
        start_event: ModelCallStartEvent,
        candidate_events: tuple[AgentEvent, ...],
        guard: Any,
    ):
        factory = self._active_run_prompt_steer_commit_guard_factory
        if guard is None or factory is None:
            yield
            return
        with factory(
            start_event=start_event,
            candidate_events=candidate_events,
            guard=guard,
            run_id=start_event.run_id,
        ):
            yield

    def validate_active_run_prompt_steer_safe_point(
        self,
        *,
        start_event: ModelCallStartEvent,
        candidate_events: tuple[AgentEvent, ...],
        guard: Any,
    ) -> None:
        validator = self._active_run_prompt_steer_safe_point_validator
        if validator is None:
            raise ValueError("active-run prompt steer owner is unavailable")
        validator(
            start_event=start_event,
            candidate_events=candidate_events,
            guard=guard,
            run_id=start_event.run_id,
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
            or self._memory_governance_reconciliation_required
            or self._publication_reconciliation_required
            or self._mandatory_audit_reconciliation_required
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

    def latch_memory_governance_reconciliation_required(self) -> None:
        """Block mutation when durable governance authority cannot be joined."""

        with self.write_coordinator.lock:
            self._memory_governance_reconciliation_required = True

    @property
    def publication_reconciliation_required(self) -> bool:
        return self._publication_reconciliation_required

    @property
    def publication_latch_generation(self) -> int:
        return self._publication_latch_generation

    def latch_publication_reconciliation_required(self) -> int:
        """Stop ordinary mutation after a critical fact could not be published."""

        with self._publication_maintenance_lock:
            if not self._publication_reconciliation_required:
                self._publication_latch_generation += 1
                self._publication_reconciliation_required = True
            return self._publication_latch_generation

    def latch_mandatory_audit_reconciliation_required(self) -> None:
        with self.write_coordinator.lock:
            self._mandatory_audit_reconciliation_required = True

    def issue_publication_terminal_maintenance_lease(
        self,
        *,
        owner_kind: PublicationTerminalMaintenanceOwnerKind,
        ordered_events: Sequence[AgentEvent],
        transaction_companion: RuntimeTransactionCompanion | None,
        deadline_budget: RuntimeEventOperationDeadlineBudget,
    ) -> PublicationTerminalMaintenanceLease:
        with self._publication_maintenance_lock:
            if not self._publication_reconciliation_required:
                raise PermissionError(
                    "terminal-maintenance lease requires an active publication latch"
                )
            return self._publication_maintenance_coordinator.issue(
                publication_latch_generation=self._publication_latch_generation,
                owner_kind=owner_kind,
                ordered_events=ordered_events,
                transaction_companion=transaction_companion,
                terminal_deadline_monotonic=(
                    deadline_budget.terminal_deadline_monotonic
                ),
            )

    def _admit_publication_terminal_maintenance(
        self,
        *,
        events: Sequence[AgentEvent],
        transaction_companion: RuntimeTransactionCompanion | None,
        deadline_monotonic: float,
        lease: object | None,
    ) -> PublicationTerminalMaintenanceAttempt | None:
        with self._publication_maintenance_lock:
            if not self._publication_reconciliation_required:
                if lease is not None:
                    raise PermissionError(
                        "publication maintenance lease used without its latch"
                    )
                return None
            if lease is None:
                raise EventReconciliationRequired(
                    "publication latch rejects ordinary runtime event mutation"
                )
            attempt = self._publication_maintenance_coordinator.admit(
                handle=lease,
                publication_latch_generation=self._publication_latch_generation,
                events=events,
                transaction_companion=transaction_companion,
                deadline_monotonic=deadline_monotonic,
            )
            _ACTIVE_PUBLICATION_MAINTENANCE_LEASE_ID.set(attempt.lease_id)
            return attempt

    def _preflight_publication_terminal_maintenance(
        self,
        *,
        events: Sequence[AgentEvent],
        transaction_companion: RuntimeTransactionCompanion | None,
        deadline_monotonic: float,
        lease: object | None,
    ) -> None:
        """Reject latched writes before projection/artifact preparation."""

        with self._publication_maintenance_lock:
            if not self._publication_reconciliation_required:
                if lease is not None:
                    raise PermissionError(
                        "publication maintenance lease used without its latch"
                    )
                return
            if lease is None:
                raise EventReconciliationRequired(
                    "publication latch rejects ordinary runtime event mutation"
                )
            self._publication_maintenance_coordinator.preflight(
                handle=lease,
                publication_latch_generation=self._publication_latch_generation,
                events=events,
                transaction_companion=transaction_companion,
                deadline_monotonic=deadline_monotonic,
            )

    def _resolve_publication_terminal_maintenance_attempt(
        self,
        attempt: PublicationTerminalMaintenanceAttempt | None,
        *,
        status: str,
    ) -> None:
        if attempt is None:
            return
        normalized: Literal["full", "none", "unknown", "partial"]
        normalized = (
            status if status in {"full", "none", "unknown", "partial"} else "unknown"
        )  # type: ignore[assignment]
        try:
            with self._publication_maintenance_lock:
                self._publication_maintenance_coordinator.resolve(
                    attempt,
                    status=normalized,
                )
        finally:
            _ACTIVE_PUBLICATION_MAINTENANCE_LEASE_ID.set(None)

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

        hard_reconciliation = (
            self._ledger_reconciliation_required
            or self._context_input_reconciliation_required
            or self._memory_governance_reconciliation_required
            or self._mandatory_audit_reconciliation_required
            or self._reconciliation_required
        )
        publication_blocked = (
            self._publication_reconciliation_required
            and _ACTIVE_PUBLICATION_MAINTENANCE_LEASE_ID.get() is None
        )
        if hard_reconciliation or publication_blocked:
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
        deadline_monotonic: float,
    ) -> EventWriteResult:
        prepared = self._prepare_event_batch(candidates)
        return await self.event_write_service.execute(
            lambda: self._confirm_and_handoff_event_batch_owned(
                prepared,
                deadline_monotonic=deadline_monotonic,
            ),
            deadline_monotonic=deadline_monotonic,
        )

    def confirm_and_handoff_event_batch_from_thread(
        self,
        candidates: Sequence[AgentEvent],
        *,
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
                deadline_monotonic=deadline,
            ),
            deadline_monotonic=deadline,
        )

    def confirm_and_handoff_event_batch(
        self,
        candidates: Sequence[AgentEvent],
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
            deadline_monotonic=deadline,
        )

    def _confirm_and_handoff_event_batch_owned(
        self,
        prepared: tuple[AgentEvent, ...],
        *,
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
                await_delivery=False,
            ).result

    def confirm_and_reduce_event_batch(
        self,
        candidates: Sequence[AgentEvent],
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
                await_delivery=False,
                enqueue_publication=False,
            ).result

    def register_committed_reducer(
        self,
        *,
        reducer_id: str,
        through_sequence: int,
        ingress: CommittedReducerIngressPort,
        rebuild_owner: CommittedReducerRebuildPort | None = None,
        post_fold: Callable[[tuple[AgentEvent, ...]], None] | None = None,
    ) -> None:
        with self.write_coordinator.lock:
            if reducer_id in self._committed_reducers:
                raise ValueError(f"Committed reducer already registered: {reducer_id}")
            registration = _CommittedReducerRegistration(
                reducer_id=reducer_id,
                through_sequence=through_sequence,
                ingress=ingress,
                rebuild_owner=(
                    rebuild_owner
                    if rebuild_owner is not None
                    else cast(CommittedReducerRebuildPort, ingress)
                    if callable(getattr(ingress, "reset_for_rebuild", None))
                    else None
                ),
                post_fold=post_fold,
            )
            # Registration is durable process state even when initial catch-up
            # fails. Keeping the failed registration is what makes an explicit
            # full reconciliation possible without reopening the session.
            self._committed_reducers[reducer_id] = registration
            try:
                current_last = self.event_log.next_sequence() - 1
                self._fold_committed_reducer_range(
                    registration,
                    through_sequence=current_last,
                    source_kind="runtime_catch_up",
                    deadline_monotonic=self._runtime_open_deadline_monotonic,
                )
            except Exception:
                registration.reconciliation_required = True
                registration.last_error = "initial committed reducer catch-up failed"
                self._reconciliation_required = True
                raise

    def _reconcile_committed_reducer_offline(self, reducer_id: str) -> None:
        """Privileged unbounded rebuild primitive for tests/offline doctor only."""

        with self.write_coordinator.lock:
            registration = self._committed_reducers[reducer_id]
            try:
                if registration.rebuild_owner is None:
                    raise RuntimeError(
                        "committed reducer has no explicit bounded rebuild owner"
                    )
                registration.rebuild_owner.reset_for_rebuild()
                registration.through_sequence = 0
                self._fold_committed_reducer_range(
                    registration,
                    through_sequence=self.event_log.next_sequence() - 1,
                    source_kind="repair",
                    deadline_monotonic=monotonic() + 30.0,
                )
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

    def _install_committed_reducer_repair(
        self,
        registration: _CommittedReducerRegistration,
        *,
        target_through_sequence: int,
        error: BaseException,
    ) -> None:
        registration.reconciliation_required = True
        registration.last_error = f"{type(error).__name__}: {_bounded_error(error)}"
        self._reconciliation_required = True
        if not registration.reducer_id.startswith(
            ("terminal_notification:", "terminal_monitor:")
        ):
            return
        self.committed_reducer_repair_service.install(
            reducer_id=registration.reducer_id,
            failed_registration_high_water=registration.through_sequence,
            target_ledger_high_water=target_through_sequence,
            last_error_code=type(error).__name__.upper(),
            recovery_base_identity=context_fingerprint(
                "terminal-projection-repair-base:v1",
                {
                    "reducer_id": registration.reducer_id,
                    "failed_through_sequence": registration.through_sequence,
                    "target_through_sequence": target_through_sequence,
                },
            ),
        )

    def _repair_committed_reducer_from_checkpoint(
        self,
        plan: CommittedReducerRepairPlan,
        deadline_monotonic: float,
    ) -> CommittedReducerRepairReceipt:
        """Rebuild one terminal reducer from its validated checkpoint + delta."""

        if plan.reducer_id.startswith("terminal_notification:"):
            from pulsara_agent.runtime.terminal.notification import (
                TERMINAL_NOTIFICATION_CHECKPOINT_KIND,
                TERMINAL_NOTIFICATION_CHECKPOINT_SCHEMA_VERSION,
                HostIngressNotificationProjectionStore,
            )

            projection_kind = TERMINAL_NOTIFICATION_CHECKPOINT_KIND
            schema_version = TERMINAL_NOTIFICATION_CHECKPOINT_SCHEMA_VERSION
            store_type = HostIngressNotificationProjectionStore
            live_store = self.terminal_notification_store
            post_fold = self._post_terminal_notification_committed_noexcept
        elif plan.reducer_id.startswith("terminal_monitor:"):
            from pulsara_agent.runtime.terminal.monitor import (
                TERMINAL_MONITOR_CHECKPOINT_KIND,
                TERMINAL_MONITOR_CHECKPOINT_SCHEMA_VERSION,
                TerminalMonitorStore,
            )

            projection_kind = TERMINAL_MONITOR_CHECKPOINT_KIND
            schema_version = TERMINAL_MONITOR_CHECKPOINT_SCHEMA_VERSION
            store_type = TerminalMonitorStore
            live_store = self.terminal_monitor_store
            post_fold = self._post_terminal_monitor_committed_noexcept
        else:
            raise OnlineReducerRepairBoundExceeded(
                "committed reducer has no online checkpoint repair binding"
            )
        checkpoint = self.event_log.read_runtime_projection_checkpoint(
            projection_kind,
            deadline_monotonic=deadline_monotonic,
        )
        if checkpoint is None:
            base_sequence = 0
            base_state = canonical_json_object_carrier(
                store_type.canonical_checkpoint_genesis_payload(
                    runtime_session_id=self.runtime_session_id
                )
            )
            candidate_store = store_type(runtime_session_id=self.runtime_session_id)
        else:
            self._validate_runtime_projection_checkpoint(
                checkpoint,
                expected_kind=projection_kind,
                expected_schema_version=schema_version,
                deadline_monotonic=deadline_monotonic,
            )
            base_sequence = checkpoint.through_sequence
            base_state = checkpoint.state
            candidate_store = store_type.from_checkpoint_payload(
                checkpoint.state_payload,
                runtime_session_id=self.runtime_session_id,
            )
        try:
            (
                delta,
                checkpoint_delta_event_count,
                checkpoint_delta_payload_bytes,
            ) = read_bounded_runtime_projection_recovery_delta(
                self.event_log,
                from_sequence_exclusive=base_sequence,
                through_sequence=plan.target_ledger_high_water,
                deadline_monotonic=deadline_monotonic,
            )
        except RuntimeProjectionRecoveryBoundExceeded as exc:
            raise OnlineReducerRepairBoundExceeded(str(exc)) from exc
        candidate_store.apply_committed(delta)
        candidate_store.through_sequence = max(
            candidate_store.through_sequence, plan.target_ledger_high_water
        )
        resulting_state = canonical_json_object_carrier(
            candidate_store.checkpoint_payload()
        )
        with self.write_coordinator.lock:
            registration = self._committed_reducers[plan.reducer_id]
            if (
                not registration.reconciliation_required
                or registration.through_sequence != plan.failed_registration_high_water
                or self.event_log.next_sequence() - 1 != plan.target_ledger_high_water
            ):
                raise EventReconciliationRequired(
                    "terminal reducer repair plan lost its exact authority"
                )
            live_store.replace_from_checkpoint_payload(resulting_state.decode_object())
            registration.through_sequence = plan.target_ledger_high_water
            registration.reconciliation_required = False
            registration.last_error = None
            fold_receipt = build_committed_reducer_fold_receipt(
                reducer_id=plan.reducer_id,
                base_through_sequence=base_sequence,
                resulting_through_sequence=plan.target_ledger_high_water,
                source_kind="restore_bootstrap",
                source_ordered_join_fingerprint=plan.plan_fingerprint,
                base_state=base_state,
                resulting_state=resulting_state,
                checkpoint_delta_event_count=checkpoint_delta_event_count,
                checkpoint_delta_payload_bytes=checkpoint_delta_payload_bytes,
            )
            registration.last_fold_receipt = fold_receipt
            self._reconciliation_required = any(
                item.reconciliation_required
                for item in self._committed_reducers.values()
            )
        try:
            self.runtime_projection_checkpoint_maintenance_service.offer(fold_receipt)
        except BaseException:
            # Checkpoint acceleration has its own stable owner and diagnostics;
            # it cannot revoke the semantic repair that was just installed.
            pass
        try:
            post_fold(
                _repair_post_fold_events(
                    delta,
                    failed_registration_high_water=(
                        plan.failed_registration_high_water
                    ),
                )
            )
        except BaseException:
            # Semantic repair is complete and authoritative at this point.
            # Process-local observers/coordinators recover independently and
            # cannot invalidate the exact reducer repair receipt.
            pass
        return build_committed_reducer_repair_receipt(
            plan=plan,
            resulting_semantic_state_fingerprint=(
                resulting_state.canonical_payload_fingerprint
            ),
        )

    def _fold_committed_reducer_range(
        self,
        registration: _CommittedReducerRegistration,
        *,
        through_sequence: int,
        source_kind: Literal["runtime_catch_up", "repair"],
        deadline_monotonic: float | None,
    ) -> None:
        """Advance one reducer through bounded historical raw-range proofs."""

        while registration.through_sequence < through_sequence:
            page_end = min(registration.through_sequence + 256, through_sequence)
            proof = self.event_log.read_joined_raw_range(
                source_kind=source_kind,
                from_sequence_exclusive=registration.through_sequence,
                through_sequence=page_end,
                max_events=256,
                max_payload_bytes=16 * 1024 * 1024,
                deadline_monotonic=deadline_monotonic,
            )
            if proof is None or proof.through_sequence != page_end:
                raise EventReconciliationRequired(
                    "committed reducer historical range is unavailable"
                )
            base_through_sequence = registration.through_sequence
            prepared_or_fold = registration.ingress.fold_restored_range(proof)
            fold_result = self._prepared_fold_receipt(prepared_or_fold)
            self._validate_committed_reducer_fold_result(
                registration,
                fold_result=fold_result,
                expected_base_through_sequence=base_through_sequence,
                expected_resulting_through_sequence=page_end,
            )
            self._commit_prepared_reducer_fold(
                registration,
                prepared_or_fold=prepared_or_fold,
            )
            # The domain ingress has installed its immutable next state.  From
            # this point to the registration-head install there are no calls
            # that may fail, so both authorities become visible under the same
            # writer lock without a store-advanced/registration-behind gap.
            registration.through_sequence = page_end
            self._settle_committed_reducer_fold(
                registration,
                fold_result=fold_result,
                events=proof.owned_stored_events,
            )

    def _apply_live_receipt_to_reducer(
        self,
        registration: _CommittedReducerRegistration,
        receipt: StoredEventBatchCommitReceipt,
        *,
        catch_up_through_sequence: int,
        deadline_monotonic: float | None,
    ) -> object | None:
        first = receipt.raw_stored_envelopes[0].sequence
        last = receipt.raw_stored_envelopes[-1].sequence
        if registration.through_sequence < first - 1:
            self._fold_committed_reducer_range(
                registration,
                through_sequence=first - 1,
                source_kind="runtime_catch_up",
                deadline_monotonic=deadline_monotonic,
            )
        live_result: object | None = None
        if registration.through_sequence == first - 1:
            base_through_sequence = registration.through_sequence
            prepared_or_fold = registration.ingress.apply_live_committed(receipt)
            live_result = self._prepared_fold_receipt(prepared_or_fold)
            self._validate_committed_reducer_fold_result(
                registration,
                fold_result=live_result,
                expected_base_through_sequence=base_through_sequence,
                expected_resulting_through_sequence=last,
            )
            self._commit_prepared_reducer_fold(
                registration,
                prepared_or_fold=prepared_or_fold,
            )
            registration.through_sequence = last
            self._settle_committed_reducer_fold(
                registration,
                fold_result=live_result,
                events=receipt.owned_stored_events,
            )
        elif registration.through_sequence < last:
            raise EventReconciliationRequired(
                "committed reducer partially overlaps a physical batch"
            )
        if registration.through_sequence < catch_up_through_sequence:
            self._fold_committed_reducer_range(
                registration,
                through_sequence=catch_up_through_sequence,
                source_kind="runtime_catch_up",
                deadline_monotonic=deadline_monotonic,
            )
        return live_result

    @staticmethod
    def _prepared_fold_receipt(prepared_or_fold: object) -> object:
        if isinstance(prepared_or_fold, PreparedCheckpointedCommittedReducerFold):
            return prepared_or_fold.fold_receipt
        return prepared_or_fold

    @staticmethod
    def _commit_prepared_reducer_fold(
        registration: _CommittedReducerRegistration,
        *,
        prepared_or_fold: object,
    ) -> None:
        if not isinstance(prepared_or_fold, PreparedCheckpointedCommittedReducerFold):
            return
        ingress = registration.ingress
        if not isinstance(ingress, CheckpointedCommittedReducerIngress):
            raise EventReconciliationRequired(
                "prepared committed reducer fold lacks its transaction owner"
            )
        ingress.commit_prepared(prepared_or_fold)

    @staticmethod
    def _validate_committed_reducer_fold_result(
        registration: _CommittedReducerRegistration,
        *,
        fold_result: object,
        expected_base_through_sequence: int,
        expected_resulting_through_sequence: int,
    ) -> None:
        """Validate typed fold authority before publishing registration head.

        Legacy reducers may return another domain-specific carrier.  A reducer
        that opts into the checkpointed fold contract, however, must prove the
        exact base and result before RuntimeSession advances its high-water.
        """

        if not isinstance(fold_result, CommittedReducerFoldReceipt):
            return
        if (
            fold_result.reducer_id != registration.reducer_id
            or fold_result.base_through_sequence != expected_base_through_sequence
            or fold_result.resulting_through_sequence
            != expected_resulting_through_sequence
        ):
            raise EventReconciliationRequired(
                "committed reducer fold receipt does not match its exact range"
            )

    def _settle_committed_reducer_fold(
        self,
        registration: _CommittedReducerRegistration,
        *,
        fold_result: object,
        events: tuple[AgentEvent, ...],
    ) -> None:
        """Advance post-fold owners without widening semantic failure scope."""

        if isinstance(fold_result, CommittedReducerFoldReceipt):
            # Exact range validation happened before the registration head was
            # installed.  Everything below is a no-fail handoff to an
            # independently owned acceleration/observation path.
            registration.last_fold_receipt = fold_result
            try:
                self.runtime_projection_checkpoint_maintenance_service.offer(
                    fold_result
                )
            except BaseException:
                # Checkpoint acceleration owns its independent diagnostics and
                # must never roll back an already installed semantic fold.
                pass
        post_fold = registration.post_fold
        if post_fold is not None:
            try:
                post_fold(events)
            except BaseException:
                # Process-local observers/coordinators repair from the fold
                # receipt/store and do not poison durable semantic authority.
                pass

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
    ) -> EventWriteResult:
        return await self.write_events(
            (event,),
            expected_last_sequence=expected_last_sequence,
        )

    async def write_event_with_deadline(
        self,
        event: AgentEvent,
        *,
        deadline_monotonic: float,
        expected_last_sequence: int | None = None,
        publication_terminal_maintenance_lease: object | None = None,
    ) -> EventWriteResult:
        return await self.write_events_with_deadline(
            (event,),
            deadline_monotonic=deadline_monotonic,
            expected_last_sequence=expected_last_sequence,
            publication_terminal_maintenance_lease=(
                publication_terminal_maintenance_lease
            ),
        )

    async def write_events(
        self,
        events: Sequence[AgentEvent],
        *,
        expected_last_sequence: int | None = None,
        transaction_companion: RuntimeTransactionCompanion | None = None,
    ) -> EventWriteResult:
        deadline = self.event_write_service.new_deadline_monotonic()
        return await self.write_events_with_deadline(
            events,
            deadline_monotonic=deadline,
            expected_last_sequence=expected_last_sequence,
            transaction_companion=transaction_companion,
        )

    async def write_events_with_deadline(
        self,
        events: Sequence[AgentEvent],
        *,
        deadline_monotonic: float,
        expected_last_sequence: int | None = None,
        transaction_companion: RuntimeTransactionCompanion | None = None,
        publication_terminal_maintenance_lease: object | None = None,
    ) -> EventWriteResult:
        if deadline_monotonic <= monotonic():
            raise EventCommitError(
                "runtime event write deadline expired before admission",
                commit_outcome="none",
                deadline_monotonic=deadline_monotonic,
            )
        self.publisher.bind_running_loop()
        self.runtime_projection_checkpoint_maintenance_service.bind_running_loop()
        self.committed_reducer_repair_service.bind_running_loop()
        self.committed_reducer_post_fold_service.bind_running_loop()
        deadline = deadline_monotonic
        self._preflight_publication_terminal_maintenance(
            events=events,
            transaction_companion=transaction_companion,
            deadline_monotonic=deadline,
            lease=publication_terminal_maintenance_lease,
        )
        prepared = await self.tool_terminal_projection_service.prepare_batch(
            tuple(events),
            deadline_monotonic=deadline,
        )
        prepared = self._prepare_event_batch(prepared)
        incoming_starts = tuple(
            event for event in prepared if isinstance(event, RunStartEvent)
        )
        if (
            incoming_starts
            and self.materialization_account_store.snapshot() is None
            and self.event_log.next_sequence() > 1
            and self.allow_unbootstrapped_test_events
        ):
            if len(incoming_starts) != 1:
                raise ValueError("test account adoption requires one RunStart")
            self._adopt_unbootstrapped_in_memory_account_for_test(
                incoming_run_id=incoming_starts[0].run_id
            )
        missing_account = self.materialization_account_store.snapshot() is None
        run_start_count = sum(isinstance(event, RunStartEvent) for event in prepared)
        is_genesis = (
            missing_account
            and run_start_count == 1
            and self.event_log.next_sequence() == 1
        )
        tool_reservation = self._active_tool_reservation_for_batch(prepared)
        mcp_continuation_companion = (
            transaction_companion
            if isinstance(transaction_companion, McpContinuationTransactionAuthority)
            else None
        )
        if transaction_companion is not None and (
            is_genesis
            or (tool_reservation is not None and mcp_continuation_companion is None)
        ):
            raise ValueError(
                "transaction companion requires an accounted one-shot event batch"
            )
        if (
            missing_account
            and not is_genesis
            and not self.allow_unbootstrapped_test_events
        ):
            raise ValueError(
                "empty ledger first batch must contain exactly one RunStartEvent"
            )
        maintenance_attempt = self._admit_publication_terminal_maintenance(
            events=events,
            transaction_companion=transaction_companion,
            deadline_monotonic=deadline,
            lease=publication_terminal_maintenance_lease,
        )
        try:
            if is_genesis:
                if expected_last_sequence not in (None, 0):
                    raise ValueError("ledger genesis expected sequence must be zero")
                attempt = await self.event_write_service.execute(
                    lambda: self._commit_genesis_reduce_enqueue(
                        prepared,
                        await_delivery=True,
                        deadline_monotonic=deadline,
                    ),
                    deadline_monotonic=deadline,
                )
            elif tool_reservation is not None:
                attempt = await self.event_write_service.execute(
                    lambda: self._charge_physical_operation_attempt_from_thread(
                        prepared,
                        reservation=tool_reservation,
                        await_delivery=True,
                        transaction_companion=mcp_continuation_companion,
                    ),
                    deadline_monotonic=deadline,
                    **self._physical_operation_continuation_admission(tool_reservation),
                )
            else:
                attempt = await self.event_write_service.execute(
                    lambda: self._commit_reduce_enqueue(
                        prepared,
                        expected_last_sequence=expected_last_sequence,
                        await_delivery=True,
                        deadline_monotonic=deadline,
                        transaction_companion=transaction_companion,
                        publication_terminal_maintenance=(
                            maintenance_attempt is not None
                        ),
                    ),
                    deadline_monotonic=deadline,
                )
        except PendingRuntimeEventWriteError as error:
            self._resolve_publication_terminal_maintenance_attempt(
                maintenance_attempt,
                status="none",
            )
            raise EventCommitError(
                str(error),
                commit_outcome="none",
                deadline_monotonic=deadline,
            ) from error
        except RuntimeEventWriteCancelled as cancelled:
            outcome = _cancelled_event_commit_outcome(cancelled)
            self._resolve_publication_terminal_maintenance_attempt(
                maintenance_attempt,
                status=outcome.status,
            )
            if outcome.status == "unknown":
                self.latch_event_commit_outcome_unknown()
            raise EventWriteCancelled(outcome) from cancelled
        except BaseException as error:
            outcome = event_batch_commit_outcome_from_error(error)
            self._resolve_publication_terminal_maintenance_attempt(
                maintenance_attempt,
                status=outcome.status if outcome is not None else "unknown",
            )
            raise
        try:
            publication_errors = await _await_publication(
                attempt.published_events,
                attempt.delivery_futures,
            )
        except asyncio.CancelledError as cancelled:
            self._resolve_publication_terminal_maintenance_attempt(
                maintenance_attempt,
                status="full",
            )
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
        except BaseException:
            self._resolve_publication_terminal_maintenance_attempt(
                maintenance_attempt,
                status="full",
            )
            raise
        result = replace(
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
        if self._batch_requires_critical_publication(
            tuple(result.committed_events)
        ) and (
            result.publication_status == "unavailable"
            or bool(result.publication_errors)
        ):
            self.latch_publication_reconciliation_required()
        self._resolve_publication_terminal_maintenance_attempt(
            maintenance_attempt,
            status="full",
        )
        return result

    @staticmethod
    def _batch_requires_critical_publication(
        events: tuple[AgentEvent, ...],
    ) -> bool:
        critical_types = (
            ToolExecutionSuspendedEvent,
            McpInputRequiredResolutionSubmittedEvent,
            McpContinuationDispatchReservedEvent,
            McpInputRequiredExpiredEvent,
            McpInputRequiredBindingChangedEvent,
            McpInputRequiredResumeFailedEvent,
            McpInputRequiredInteractionClosedEvent,
            ContextCompactionRequestedEvent,
            MidTurnContextCompactionSkippedEvent,
            ToolResultEvidenceProjectionFailedEvent,
            ContextCompactionStartedEvent,
            ContextCompactionCompletedEvent,
            ContextCompactionFailedEvent,
        )
        return any(
            isinstance(event, critical_types)
            or (
                isinstance(event, ToolResultEndEvent)
                and event.mcp_input_required_terminal_source is not None
            )
            or (
                isinstance(event, RunEndEvent)
                and (
                    event.mcp_input_required_closure_event_reference is not None
                    or event.publication_latched_termination is not None
                )
            )
            for event in events
        )

    @classmethod
    def _batch_requires_publication_confirmation(
        cls,
        events: tuple[AgentEvent, ...],
    ) -> bool:
        """Return whether this producer must observe publication failure.

        Run-boundary and MCP-installation producers terminalize their already
        committed authority themselves.  They therefore require the typed
        publication error without installing the global publication latch,
        which is reserved for facts whose only legal successor uses the
        terminal-maintenance lease protocol.
        """

        return cls._batch_requires_critical_publication(events) or any(
            isinstance(event, (RunStartEvent, McpCapabilitySnapshotInstalledEvent))
            for event in events
        )

    def write_events_from_thread(
        self,
        events: Sequence[AgentEvent],
        *,
        expected_last_sequence: int | None = None,
        deadline_monotonic: float | None = None,
        transaction_companion: RuntimeTransactionCompanion | None = None,
    ) -> EventWriteResult:
        self._require_tool_terminal_projection_ready(events)
        prepared = self._prepare_event_batch(events)
        if (
            self.materialization_account_store.snapshot() is None
            and not self.allow_unbootstrapped_test_events
        ):
            if self.event_log.next_sequence() == 1:
                raise ValueError(
                    "fresh durable ledger genesis must use the async RunStart path"
                )
            self._latch_ledger_reconciliation_required()
            raise EventReconciliationRequired(
                "non-empty durable ledger lacks its materialization account"
            )
        owned_deadline = (
            self.event_write_service.current_deadline_monotonic()
            if self.event_write_service.is_current_owner()
            else self.event_write_service.new_deadline_monotonic()
        )
        deadline = (
            owned_deadline
            if deadline_monotonic is None
            else min(owned_deadline, deadline_monotonic)
        )
        tool_reservation = self._active_tool_reservation_for_batch(prepared)
        if tool_reservation is not None:
            if transaction_companion is not None:
                raise ValueError(
                    "tool physical continuation cannot carry a transaction companion"
                )
            return self.event_write_service.execute_blocking(
                lambda: self.charge_physical_operation_from_thread(
                    prepared,
                    reservation=tool_reservation,
                ),
                deadline_monotonic=deadline,
                **self._physical_operation_continuation_admission(tool_reservation),
            )
        return self.event_write_service.execute_blocking(
            lambda: (
                self._commit_reduce_enqueue(
                    prepared,
                    expected_last_sequence=expected_last_sequence,
                    await_delivery=False,
                    deadline_monotonic=deadline,
                    transaction_companion=transaction_companion,
                ).result
            ),
            deadline_monotonic=deadline,
        )

    def _active_tool_reservation_for_batch(
        self,
        events: Sequence[AgentEvent],
    ) -> PhysicalOperationReservationFact | None:
        tool_events = (
            ToolResultStartEvent,
            ToolResultTextDeltaEvent,
            ToolResultDataDeltaEvent,
        )
        if not events:
            return None
        if all(isinstance(event, tool_events) for event in events):
            tool_call_ids = {event.tool_call_id for event in events}
        else:
            resolutions = tuple(
                event
                for event in events
                if isinstance(event, McpInputRequiredResolutionSubmittedEvent)
            )
            dispatches = tuple(
                event
                for event in events
                if isinstance(event, McpContinuationDispatchReservedEvent)
            )
            if len(resolutions) == 1 and not dispatches:
                tool_call_ids = {resolutions[0].source.interaction.tool_call_id}
            elif len(dispatches) == 1 and len(events) == 1:
                physical = dispatches[0].dispatch_reservation.physical_operation_id
                matches = tuple(
                    reservation
                    for (
                        kind,
                        _,
                    ), reservation in self._physical_reservation_facts.items()
                    if kind is PhysicalOperationKind.TOOL_CALL
                    and reservation.reservation_id == physical
                )
                if len(matches) != 1:
                    return None
                return matches[0]
            else:
                return None
        if len(tool_call_ids) != 1:
            raise ValueError(
                "one tool-operation charge batch must have one tool call identity"
            )
        tool_call_id = next(iter(tool_call_ids))
        return self.physical_reservation_for_owner(
            operation_kind=PhysicalOperationKind.TOOL_CALL,
            owner_id=tool_call_id,
        )

    def _physical_operation_continuation_admission(
        self,
        reservation: PhysicalOperationReservationFact,
    ) -> dict[str, Any]:
        return {
            "admission_class": LedgerWriteAdmissionClass.OPERATION_CONTINUATION,
            "operation_owner_id": self.physical_operation_admission_owner_id(
                operation_kind=reservation.owner_kind,
                owner_id=reservation.owner_id,
            ),
        }

    def physical_dispatch_capacity(
        self,
        operation_kind: PhysicalOperationKind,
    ) -> int:
        contract = self.authority_materialization_contracts.burst_registry.unique_binding_for_operation(
            operation_kind
        ).contract
        return self.materialization_coordinator.available_dispatch_capacity(
            burst_contract=contract
        )

    async def ensure_physical_operation_headroom(
        self,
        operation_kind: PhysicalOperationKind,
    ) -> None:
        """Advance the current minimum consumer before dispatching an owner."""

        if self.materialization_account_store.snapshot() is None:
            if self.allow_unbootstrapped_test_events:
                return
            raise ValueError(
                "physical operation requires a bootstrapped materialization account"
            )
        while self.physical_dispatch_capacity(operation_kind) <= 0:
            account = self.materialization_account_store.snapshot()
            if account is None:
                raise RuntimeError(
                    "materialization account disappeared during recovery"
                )
            minimum = account.generation.reclaimable_through_sequence
            blockers = tuple(
                item
                for item in account.generation.consumer_horizons
                if item.through_sequence == minimum
            )
            if not blockers:
                break
            source_revision = (
                account.generation.ledger_materialization_generation,
                account.generation.consumer_horizon_revision,
                minimum,
            )
            for blocker in blockers:
                if (
                    blocker.consumer_kind
                    is LedgerMaterializationConsumerKind.TRANSCRIPT_WINDOW
                ):
                    await self.transcript_projection_checkpoint_service.checkpoint_for_admission(
                        operation_kind=operation_kind
                    )
                elif (
                    blocker.consumer_kind
                    is LedgerMaterializationConsumerKind.SUBAGENT_GRAPH
                ):
                    await (
                        self.subagent_graph_checkpoint_service.checkpoint_for_admission(
                            requested_through_sequence=account.ledger_through_sequence
                        )
                    )
                if self.physical_dispatch_capacity(operation_kind) > 0:
                    return
            updated = self.materialization_account_store.snapshot()
            if updated is None or source_revision == (
                updated.generation.ledger_materialization_generation,
                updated.generation.consumer_horizon_revision,
                updated.generation.reclaimable_through_sequence,
            ):
                break

        if self.physical_dispatch_capacity(operation_kind) > 0:
            return
        from pulsara_agent.runtime.authority_materialization import (
            PhysicalHeadroomExceeded,
        )

        raise PhysicalHeadroomExceeded(
            "physical headroom remains exhausted after minimum-consumer recovery"
        )

    def reserve_physical_operation_from_thread(
        self,
        events: Sequence[AgentEvent],
        *,
        operation_kind: PhysicalOperationKind,
        reservation_id: str,
        owner_id: str,
        transaction_companion: EventLogTransactionCompanion | None = None,
    ) -> tuple[PhysicalOperationReservationFact, EventWriteResult]:
        """Atomically commit a dispatch proof and retain its physical reserve."""

        if not self.event_write_service.is_current_owner():
            raise RuntimeError(
                "physical dispatch reservation requires the critical writer owner"
            )
        prepared = self._prepare_event_batch(events)
        if not prepared:
            raise ValueError("physical dispatch reservation requires business facts")
        deadline = self.event_write_service.current_deadline_monotonic()
        with self._host_ingress_commit_guard(prepared), self.write_coordinator.lock:
            self.require_mutation_allowed()
            self._validate_run_lifecycle_batch(prepared)
            self.long_horizon_state_store.validate_next_batch(prepared)
            first = prepared[0]
            try:
                committed = self.materialization_coordinator.reserve_and_commit_dispatch(
                    context=EventContext(
                        run_id=first.run_id,
                        turn_id=first.turn_id,
                        reply_id=first.reply_id,
                    ),
                    business_events=prepared,
                    reservation_id=reservation_id,
                    owner_id=owner_id,
                    burst_contract=(
                        self.authority_materialization_contracts.burst_registry.unique_binding_for_operation(
                            operation_kind
                        ).contract
                    ),
                    deadline_monotonic=deadline,
                    transaction_companion=transaction_companion,
                )
            except BaseException as exc:
                self._raise_materialization_commit_error(
                    exc,
                    deadline_monotonic=deadline,
                )
                raise AssertionError("unreachable materialization exception mapping")
            key = (committed.reservation.owner_kind, committed.reservation.owner_id)
            promoted = self.event_write_service.promote_current_producer_admission(
                operation_owner_ids=(_physical_operation_admission_owner_id(key),),
            )
            if len(promoted) != 1 or key in self._physical_operation_admission_tokens:
                self._latch_ledger_reconciliation_required()
                raise EventReconciliationRequired(
                    "physical dispatch admission promotion is ambiguous"
                )
            self._physical_operation_admission_tokens[key] = promoted[0]
            result = self._handoff_accounted_business_batch(
                stored_batch_receipt=committed.stored_batch_receipt,
                business_events=prepared,
                deadline_monotonic=deadline,
            )
            physical_reservation = committed.reservation
            key = (physical_reservation.owner_kind, physical_reservation.owner_id)
            existing = self._physical_reservation_facts.get(key)
            if existing is not None and existing != physical_reservation:
                self._latch_ledger_reconciliation_required()
                raise EventReconciliationRequired(
                    "process-local physical reservation owner is ambiguous"
                )
            self._physical_reservation_facts[key] = physical_reservation
            return committed.reservation, result

    def commit_graph_checkpoint_from_thread(
        self,
        event: AgentEvent,
        *,
        ledger_charged_payload_bytes_through_checkpoint: int,
        ledger_continuity_accumulator_through_checkpoint: str,
    ) -> EventWriteResult:
        """Atomically commit a graph checkpoint and its shared-ledger horizon."""

        from pulsara_agent.event import SubagentGraphCheckpointCommittedEvent

        if not self.event_write_service.is_current_owner():
            raise RuntimeError("graph checkpoint commit requires the critical writer")
        if not isinstance(event, SubagentGraphCheckpointCommittedEvent):
            raise TypeError("graph checkpoint commit requires its typed event")
        prepared = self._prepare_event_batch((event,))
        checkpoint_event = prepared[0]
        assert isinstance(checkpoint_event, SubagentGraphCheckpointCommittedEvent)
        deadline = self.event_write_service.current_deadline_monotonic()
        with self.write_coordinator.lock:
            self.require_mutation_allowed()
            self._validate_run_lifecycle_batch(prepared)
            self.long_horizon_state_store.validate_next_batch(prepared)
            try:
                committed = self.materialization_coordinator.commit_graph_checkpoint_consumer_advance(
                    checkpoint_event=checkpoint_event,
                    ledger_charged_payload_bytes_through_checkpoint=(
                        ledger_charged_payload_bytes_through_checkpoint
                    ),
                    ledger_continuity_accumulator_through_checkpoint=(
                        ledger_continuity_accumulator_through_checkpoint
                    ),
                    deadline_monotonic=deadline,
                )
            except BaseException as exc:
                self._raise_materialization_commit_error(
                    exc,
                    deadline_monotonic=deadline,
                )
                raise AssertionError("unreachable materialization exception mapping")
            return self._handoff_accounted_business_batch(
                stored_batch_receipt=committed.stored_batch_receipt,
                business_events=(checkpoint_event,),
                deadline_monotonic=deadline,
            )

    def charge_physical_operation_from_thread(
        self,
        events: Sequence[AgentEvent],
        *,
        reservation: PhysicalOperationReservationFact,
    ) -> EventWriteResult:
        """Charge one stable non-terminal batch to an active reservation."""

        return self._charge_physical_operation_attempt_from_thread(
            events,
            reservation=reservation,
            await_delivery=False,
        ).result

    def _charge_physical_operation_attempt_from_thread(
        self,
        events: Sequence[AgentEvent],
        *,
        reservation: PhysicalOperationReservationFact,
        await_delivery: bool,
        transaction_companion: McpContinuationTransactionAuthority | None = None,
    ) -> _WriteAttempt:
        """Charge a batch while preserving its ordered publication waiters."""

        if not self.event_write_service.is_current_owner():
            raise RuntimeError("physical charge requires the critical writer owner")
        prepared = self._prepare_event_batch(events)
        deadline = self.event_write_service.current_deadline_monotonic()
        with self.write_coordinator.lock:
            self.require_mutation_allowed()
            self._validate_run_lifecycle_batch(prepared)
            self.long_horizon_state_store.validate_next_batch(prepared)
            first = prepared[0]
            burst_contract = self.authority_materialization_contracts.burst_registry.unique_binding_for_operation(
                reservation.owner_kind
            ).contract
            retained_tail_events = (
                burst_contract.minimum_terminal_tail_events
                if isinstance(burst_contract, ToolDeltaBurstContractFact)
                else reservation.terminal_tail_reserved_events
            )
            retained_tail_payload_bytes = (
                burst_contract.minimum_terminal_tail_payload_bytes
                if isinstance(burst_contract, ToolDeltaBurstContractFact)
                else reservation.terminal_tail_reserved_payload_bytes
            )
            try:
                committed = self.materialization_coordinator.commit_reserved_charge(
                    context=EventContext(
                        run_id=first.run_id,
                        turn_id=first.turn_id,
                        reply_id=first.reply_id,
                    ),
                    reservation=reservation,
                    business_events=prepared,
                    companion_payload_charge_bytes=(
                        transaction_companion.charged_payload_bytes
                        if transaction_companion is not None
                        else 0
                    ),
                    companion_charge_contract_fingerprint=(
                        transaction_companion.charge_contract_fingerprint
                        if transaction_companion is not None
                        else None
                    ),
                    retained_terminal_tail_events=retained_tail_events,
                    retained_terminal_tail_payload_bytes=(retained_tail_payload_bytes),
                    deadline_monotonic=deadline,
                    transaction_companion=transaction_companion,
                )
            except BaseException as exc:
                self._raise_materialization_commit_error(
                    exc,
                    deadline_monotonic=deadline,
                )
                raise AssertionError("unreachable materialization exception mapping")
            attempt = self._handoff_accounted_business_batch_attempt(
                stored_batch_receipt=committed.stored_batch_receipt,
                business_events=prepared,
                deadline_monotonic=deadline,
                await_delivery=await_delivery,
            )
            key = (reservation.owner_kind, reservation.owner_id)
            if self._physical_reservation_facts.get(key) != reservation:
                self._latch_ledger_reconciliation_required()
                raise EventReconciliationRequired(
                    "charged physical reservation registry identity drifted"
                )
            return attempt

    def reserve_physical_operation_batch_from_thread(
        self,
        events: Sequence[AgentEvent],
        *,
        dispatch_requests: Sequence[Any],
        one_shot_request: Any | None = None,
    ) -> tuple[tuple[PhysicalOperationReservationFact, ...], EventWriteResult]:
        """Atomically admit independent physical owners for one business batch."""

        if not self.event_write_service.is_current_owner():
            raise RuntimeError(
                "physical dispatch batch requires the critical writer owner"
            )
        prepared = self._prepare_event_batch(events)
        if not prepared:
            raise ValueError("physical dispatch batch requires business facts")
        deadline = self.event_write_service.current_deadline_monotonic()
        with self.write_coordinator.lock:
            self.require_mutation_allowed()
            self._validate_run_lifecycle_batch(prepared)
            self.long_horizon_state_store.validate_next_batch(prepared)
            first = prepared[0]
            try:
                committed = (
                    self.materialization_coordinator.reserve_and_commit_dispatch_batch(
                        context=EventContext(
                            run_id=first.run_id,
                            turn_id=first.turn_id,
                            reply_id=first.reply_id,
                        ),
                        business_events=prepared,
                        dispatch_requests=dispatch_requests,
                        one_shot_request=one_shot_request,
                        deadline_monotonic=deadline,
                    )
                )
            except BaseException as exc:
                self._raise_materialization_commit_error(
                    exc,
                    deadline_monotonic=deadline,
                )
                raise AssertionError("unreachable materialization exception mapping")
            operation_keys = tuple(
                (reservation.owner_kind, reservation.owner_id)
                for reservation in committed.reservations
            )
            promoted = self.event_write_service.promote_current_producer_admission(
                operation_owner_ids=tuple(
                    _physical_operation_admission_owner_id(key)
                    for key in operation_keys
                ),
            )
            if len(promoted) != len(operation_keys) or any(
                key in self._physical_operation_admission_tokens
                for key in operation_keys
            ):
                self._latch_ledger_reconciliation_required()
                raise EventReconciliationRequired(
                    "physical dispatch batch admission promotion is ambiguous"
                )
            self._physical_operation_admission_tokens.update(
                zip(operation_keys, promoted, strict=True)
            )
            result = self._handoff_accounted_business_batch(
                stored_batch_receipt=committed.stored_batch_receipt,
                business_events=prepared,
                deadline_monotonic=deadline,
            )
            for reservation in committed.reservations:
                key = (reservation.owner_kind, reservation.owner_id)
                existing = self._physical_reservation_facts.get(key)
                if existing is not None and existing != reservation:
                    self._latch_ledger_reconciliation_required()
                    raise EventReconciliationRequired(
                        "physical dispatch batch registry identity is ambiguous"
                    )
                self._physical_reservation_facts[key] = reservation
            return committed.reservations, result

    def settle_physical_operation_from_thread(
        self,
        events: Sequence[AgentEvent],
        *,
        reservation: PhysicalOperationReservationFact,
        terminal_outcome: str,
        model_stream_measurement_fingerprint: str | None = None,
        transaction_companion: RuntimeTransactionCompanion | None = None,
    ) -> EventWriteResult:
        """Commit a stable terminal batch and remove its exact reserve."""

        if not self.event_write_service.is_current_owner():
            raise RuntimeError("physical settlement requires the critical writer owner")
        prepared = self._prepare_event_batch(events)
        deadline = self.event_write_service.current_deadline_monotonic()
        with self.write_coordinator.lock:
            self.require_mutation_allowed()
            self._validate_run_lifecycle_batch(prepared)
            self.long_horizon_state_store.validate_next_batch(prepared)
            first = prepared[0]
            try:
                committed = self.materialization_coordinator.commit_reserved_settlement(
                    context=EventContext(
                        run_id=first.run_id,
                        turn_id=first.turn_id,
                        reply_id=first.reply_id,
                    ),
                    reservation=reservation,
                    business_events=prepared,
                    terminal_outcome=terminal_outcome,
                    model_stream_measurement_fingerprint=(
                        model_stream_measurement_fingerprint
                    ),
                    deadline_monotonic=deadline,
                    transaction_companion=transaction_companion,
                )
            except BaseException as exc:
                self._raise_materialization_commit_error(
                    exc,
                    deadline_monotonic=deadline,
                )
                raise AssertionError("unreachable materialization exception mapping")
            key = (reservation.owner_kind, reservation.owner_id)
            operation_token = self._physical_operation_admission_tokens.pop(key, None)
            if operation_token is None:
                self._latch_ledger_reconciliation_required()
                raise EventReconciliationRequired(
                    "settled physical reservation lost its operation admission"
                )
            self.checkpoint_dispatch_barrier_coordinator.release_write_admission(
                operation_token
            )
            result = self._handoff_accounted_business_batch(
                stored_batch_receipt=committed.stored_batch_receipt,
                business_events=prepared,
                deadline_monotonic=deadline,
            )
            key = (reservation.owner_kind, reservation.owner_id)
            if self._physical_reservation_facts.get(key) != reservation:
                self._latch_ledger_reconciliation_required()
                raise EventReconciliationRequired(
                    "settled physical reservation registry identity drifted"
                )
            self._physical_reservation_facts.pop(key)
            return result

    def suspend_physical_operation_from_thread(
        self,
        events: Sequence[AgentEvent],
        *,
        reservation: PhysicalOperationReservationFact,
        suspension_id: str,
        binding_identity_fingerprint: str,
        transaction_companion: McpContinuationTransactionAuthority | None = None,
    ) -> EventWriteResult:
        """Commit a suspension batch and retain only its exact physical tail."""

        if not self.event_write_service.is_current_owner():
            raise RuntimeError("physical suspension requires the critical writer owner")
        prepared = self._prepare_event_batch(events)
        deadline = self.event_write_service.current_deadline_monotonic()
        with self.write_coordinator.lock:
            self.require_mutation_allowed()
            self._validate_run_lifecycle_batch(prepared)
            self.long_horizon_state_store.validate_next_batch(prepared)
            first = prepared[0]
            burst_contract = self.authority_materialization_contracts.burst_registry.unique_binding_for_operation(
                reservation.owner_kind
            ).contract
            if not isinstance(burst_contract, ToolDeltaBurstContractFact):
                raise TypeError("physical suspension requires a tool burst contract")
            try:
                committed = self.materialization_coordinator.commit_reserved_suspension(
                    context=EventContext(
                        run_id=first.run_id,
                        turn_id=first.turn_id,
                        reply_id=first.reply_id,
                    ),
                    reservation=reservation,
                    burst_contract=burst_contract,
                    business_events=prepared,
                    suspension_id=suspension_id,
                    binding_identity_fingerprint=binding_identity_fingerprint,
                    companion_payload_charge_bytes=(
                        transaction_companion.charged_payload_bytes
                        if transaction_companion is not None
                        else 0
                    ),
                    companion_charge_contract_fingerprint=(
                        transaction_companion.charge_contract_fingerprint
                        if transaction_companion is not None
                        else None
                    ),
                    deadline_monotonic=deadline,
                    transaction_companion=transaction_companion,
                )
            except BaseException as exc:
                self._raise_materialization_commit_error(
                    exc,
                    deadline_monotonic=deadline,
                )
                raise AssertionError("unreachable materialization exception mapping")
            result = self._handoff_accounted_business_batch(
                stored_batch_receipt=committed.stored_batch_receipt,
                business_events=prepared,
                deadline_monotonic=deadline,
            )
            key = (reservation.owner_kind, reservation.owner_id)
            if self._physical_reservation_facts.get(key) != reservation:
                self._latch_ledger_reconciliation_required()
                raise EventReconciliationRequired(
                    "suspended physical reservation registry identity drifted"
                )
            return result

    def _handoff_accounted_business_batch(
        self,
        *,
        stored_batch_receipt: StoredEventBatchCommitReceipt,
        business_events: tuple[AgentEvent, ...],
        deadline_monotonic: float,
    ) -> EventWriteResult:
        return self._handoff_accounted_business_batch_attempt(
            stored_batch_receipt=stored_batch_receipt,
            business_events=business_events,
            deadline_monotonic=deadline_monotonic,
        ).result

    def _handoff_accounted_business_batch_attempt(
        self,
        *,
        stored_batch_receipt: StoredEventBatchCommitReceipt,
        business_events: tuple[AgentEvent, ...],
        deadline_monotonic: float,
        await_delivery: bool = False,
    ) -> _WriteAttempt:
        stored_events = stored_batch_receipt.owned_stored_events
        full_attempt = self._reconcile_confirmed_attempt(
            stored_batch_receipt,
            catch_up_through_sequence=_event_sequence(stored_events[-1]),
            await_delivery=await_delivery,
        )
        by_id = {event.id: event for event in stored_events}
        try:
            committed_business = tuple(by_id[event.id] for event in business_events)
        except KeyError as exc:
            self._latch_ledger_reconciliation_required()
            raise EventReconciliationRequired(
                "materialization batch lost one of its business facts"
            ) from exc
        attempt = _WriteAttempt(
            result=full_attempt.result.with_partition(
                business_events=committed_business,
                accounting_events=tuple(
                    event
                    for event in stored_events
                    if event.id not in {item.id for item in business_events}
                ),
            ),
            delivery_futures=full_attempt.delivery_futures,
            published_events=full_attempt.published_events,
        )
        if self._batch_requires_critical_publication(committed_business) and (
            attempt.result.publication_status == "unavailable"
            or bool(attempt.result.publication_errors)
        ):
            self.latch_publication_reconciliation_required()
        return attempt

    def _raise_materialization_commit_error(
        self,
        exc: BaseException,
        *,
        deadline_monotonic: float,
    ) -> None:
        from pulsara_agent.runtime.authority_materialization import (
            MaterializationAccountCommitFailed,
            MaterializationAccountReconciliationRequired,
        )

        if isinstance(exc, MaterializationAccountReconciliationRequired):
            self._latch_ledger_reconciliation_required()
            raise EventCommitError(
                "materialization event/account outcome is unknown",
                commit_outcome="unknown",
                deadline_monotonic=deadline_monotonic,
            ) from exc
        if isinstance(exc, MaterializationAccountCommitFailed):
            raise EventCommitError(
                "materialization event/account batch was not committed",
                commit_outcome="none",
                deadline_monotonic=deadline_monotonic,
            ) from exc
        raise exc

    def commit_reduce_events_from_thread(
        self,
        events: Sequence[AgentEvent],
        *,
        expected_last_sequence: int | None = None,
    ) -> EventWriteResult:
        """Commit and synchronously fold while deferring ordered publication."""

        self._require_tool_terminal_projection_ready(events)
        prepared = self._prepare_event_batch(events)
        deadline = (
            self.event_write_service.current_deadline_monotonic()
            if self.event_write_service.is_current_owner()
            else self.event_write_service.new_deadline_monotonic()
        )
        return self.event_write_service.execute_blocking(
            lambda: (
                self._commit_reduce_enqueue(
                    prepared,
                    expected_last_sequence=expected_last_sequence,
                    await_delivery=False,
                    enqueue_publication=False,
                    deadline_monotonic=deadline,
                ).result
            ),
            deadline_monotonic=deadline,
        )

    def publish_committed_through_from_thread(
        self,
        *,
        through_sequence: int,
    ) -> Literal["completed", "enqueued", "unavailable"]:
        """Enqueue a contiguous committed prefix after control linearization."""

        with self.write_coordinator.lock:
            enqueue = self._catch_up_publisher(
                through_sequence=through_sequence,
                await_delivery=False,
            )
        return _publication_status(enqueue)

    def _prepare_event_batch(
        self, events: Sequence[AgentEvent]
    ) -> tuple[AgentEvent, ...]:
        return tuple(self.prepare_event_for_write(event) for event in events)

    @staticmethod
    def _require_tool_terminal_projection_ready(
        events: Sequence[AgentEvent],
    ) -> None:
        from pulsara_agent.runtime.terminal_projection import ToolResultEndCandidate

        if any(isinstance(event, ToolResultEndCandidate) for event in events):
            raise ValueError(
                "thread writer requires a prepared tool terminal projection"
            )

    def prepare_event_for_write(self, event: AgentEvent) -> AgentEvent:
        """Return the exact event value owned by this session's write boundary."""

        self._require_runtime_managed_sequence(event)
        return self._with_default_metadata(event)

    def _validate_memory_governance_batch(
        self,
        events: tuple[AgentEvent, ...],
    ) -> None:
        from pulsara_agent.event import (
            MemoryGovernanceBatchBlockedEvent,
            MemoryGovernanceBatchCompletedEvent,
            MemoryGovernanceBatchFailedEvent,
            MemoryGovernanceBatchPreparedEvent,
        )
        from pulsara_agent.memory.governance.batch_input import (
            hydrate_governance_batch_input,
        )
        from pulsara_agent.llm.terminal_projection import stable_event_identity

        terminal_types = (
            MemoryGovernanceBatchCompletedEvent,
            MemoryGovernanceBatchFailedEvent,
            MemoryGovernanceBatchBlockedEvent,
        )
        for event in events:
            if (
                isinstance(event, ModelCallStartEvent)
                and event.governance_input_attribution is not None
            ):
                attribution = event.governance_input_attribution
                prepared_ref = attribution.governance_batch_prepared_event_reference
                prepared_event = self.event_log.get_by_id(
                    prepared_ref.stable_identity.event_id
                )
                if not isinstance(
                    prepared_event, MemoryGovernanceBatchPreparedEvent
                ) or prepared_ref.stable_identity != stable_event_identity(
                    prepared_event,
                    runtime_session_id=self.runtime_session_id,
                ):
                    raise ValueError(
                        "governance model Start lacks its exact Prepared event"
                    )
                snapshot = hydrate_governance_batch_input(
                    reference=attribution.batch_input_reference,
                    archive=self.archive,
                    runtime_session_id=self.runtime_session_id,
                    deadline_monotonic=(
                        self.event_write_service.current_deadline_monotonic()
                    ),
                )
                model_input = snapshot.model_input
                if (
                    prepared_event.batch_input_reference
                    != attribution.batch_input_reference
                    or event.resolved_call != model_input.resolved_call
                    or event.context_id != model_input.context_id
                    or attribution.final_model_visible_input_fingerprint
                    != model_input.provider_neutral_context_fingerprint
                ):
                    raise ValueError("governance model Start/artifact identity drifted")
            elif isinstance(event, MemoryGovernanceBatchPreparedEvent):
                snapshot = hydrate_governance_batch_input(
                    reference=event.batch_input_reference,
                    archive=self.archive,
                    runtime_session_id=self.runtime_session_id,
                    deadline_monotonic=(
                        self.event_write_service.current_deadline_monotonic()
                    ),
                )
                expected_prompt_fingerprint = context_fingerprint(
                    "governance-ordered-prompt-projections:v1",
                    {
                        "evidence": tuple(
                            item.prompt_projection.projection_fingerprint
                            for item in snapshot.ordered_candidate_snapshots
                        ),
                        "relatedness": tuple(
                            item.snapshot_fingerprint
                            for item in snapshot.ordered_relatedness_snapshots
                        ),
                    },
                )
                expected_payload = {
                    "governance_batch_id": snapshot.governance_batch_id,
                    "source_ledger_through_sequence": (
                        snapshot.source_ledger_through_sequence
                    ),
                    "candidate_entry_ids": tuple(
                        item.candidate_entry_id
                        for item in snapshot.ordered_preparing_claims
                    ),
                    "preparing_claims_fingerprint": context_fingerprint(
                        "governance-preparing-claims:v1",
                        tuple(
                            item.claim_fingerprint
                            for item in snapshot.ordered_preparing_claims
                        ),
                    ),
                    "batch_input_reference": event.batch_input_reference,
                    "resolved_model_call_id": (
                        snapshot.model_input.resolved_call.resolved_model_call_id
                    ),
                    "target_fingerprint": snapshot.model_input.target_fingerprint,
                    "model_input_fingerprint": (
                        snapshot.model_input.provider_neutral_context_fingerprint
                    ),
                    "ordered_prompt_projections_fingerprint": (
                        expected_prompt_fingerprint
                    ),
                }
                if (
                    event.governance_batch_id != snapshot.governance_batch_id
                    or event.source_ledger_through_sequence
                    != snapshot.source_ledger_through_sequence
                    or event.candidate_entry_ids
                    != expected_payload["candidate_entry_ids"]
                    or event.preparing_claims_fingerprint
                    != expected_payload["preparing_claims_fingerprint"]
                    or event.resolved_model_call_id
                    != expected_payload["resolved_model_call_id"]
                    or event.target_fingerprint
                    != expected_payload["target_fingerprint"]
                    or event.model_input_fingerprint
                    != expected_payload["model_input_fingerprint"]
                    or event.ordered_prompt_projections_fingerprint
                    != expected_prompt_fingerprint
                    or event.event_fingerprint
                    != context_fingerprint(
                        "memory-governance-batch-prepared-event:v1",
                        expected_payload,
                    )
                ):
                    raise ValueError("governance Prepared event/artifact join drifted")
            elif isinstance(event, terminal_types):
                prepared = self.event_log.get_by_id(event.prepared_event_id)
                if not isinstance(prepared, MemoryGovernanceBatchPreparedEvent):
                    raise ValueError(
                        "governance terminal event lacks its Prepared authority"
                    )
                if (
                    prepared.governance_batch_id != event.governance_batch_id
                    or prepared.batch_input_reference.batch_input_fingerprint
                    != event.batch_input_fingerprint
                ):
                    raise ValueError("governance terminal/Prepared identity drifted")
                terminal_kind = (
                    "completed"
                    if isinstance(event, MemoryGovernanceBatchCompletedEvent)
                    else "failed"
                    if isinstance(event, MemoryGovernanceBatchFailedEvent)
                    else "blocked"
                )
                payload = {
                    "governance_batch_id": event.governance_batch_id,
                    "prepared_event_id": event.prepared_event_id,
                    "batch_input_fingerprint": event.batch_input_fingerprint,
                    "governance_model_call_id": event.governance_model_call_id,
                    "decision_ids": event.decision_ids,
                    "terminal_reason": event.terminal_reason,
                    "diagnostics": event.diagnostics,
                }
                if event.terminal_event_fingerprint != context_fingerprint(
                    f"memory-governance-batch-{terminal_kind}-event:v1",
                    payload,
                ):
                    raise ValueError("governance terminal event fingerprint drifted")

    def _validate_run_lifecycle_batch(self, events: tuple[AgentEvent, ...]) -> None:
        """Enforce the stable RunStart-to-RunEnd identity before commit."""

        self.mcp_input_required_lifecycle_store.validate_next_batch(events)
        self._validate_memory_governance_batch(events)
        self._validate_compaction_extension_batch(events)
        self._validate_terminal_projection_batch(events)
        self._validate_terminal_monitor_registration_batch(events)
        self._validate_terminal_monitor_cancellation_batch(events)
        self.terminal_monitor_store.validate_receipt_application_batch(events)
        self.terminal_notification_store.validate_candidate_batch(events)

        if self._host_ingress_commit_validator is not None:
            for run_start in (
                item for item in events if isinstance(item, RunStartEvent)
            ):
                if run_start.run_entry_kind.value == "host":
                    self._host_ingress_commit_validator(run_start)

        if not self.allow_unbootstrapped_test_events:
            for event in events:
                if (
                    isinstance(event, ContextCompiledEvent)
                    and event.status == "compiled"
                    and event.prepared_provider_input is None
                ):
                    raise ValueError(
                        "production compiled context requires prepared provider input"
                    )
                if (
                    isinstance(event, ModelCallStartEvent)
                    and event.provider_input_reference is None
                ):
                    raise ValueError(
                        "production ModelStart requires committed provider input"
                    )

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
            resolving_call_ids = {
                event.resolved_model_call_id
                for event in events
                if isinstance(event, ModelCallControlDispositionResolvedEvent)
                and event.run_id == terminal.run_id
            }
            unresolved_call_ids = (
                set(
                    self.transcript_projection_state_store.unresolved_completed_call_ids(
                        terminal.run_id
                    )
                )
                - resolving_call_ids
            )
            if unresolved_call_ids:
                raise ValueError(
                    "RunEnd requires FULL model control disposition commit: "
                    + ", ".join(sorted(unresolved_call_ids))
                )
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
                    decode_raw_stored_event_envelope(
                        item, DEFAULT_EVENT_SCHEMA_REGISTRY
                    )
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
            validate_publication_latched_run_termination_authority(
                terminal,
                runtime_session_id=self.runtime_session_id,
                resolve_event=self.event_log.get_by_id,
            )
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
                    if isinstance(existing_started, ContextWindowCompactionStartedEvent)
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
                    or not isinstance(settlement, RolloutBudgetReservationSettledEvent)
                    or settlement.reservation_id
                    != started.plan.rollout_reservation.reservation_id
                    or settlement.usage_charge is None
                    or reservation_quote is None
                    or settlement.usage_charge.reservation_quote_fact_fingerprint
                    != reservation_quote.quote_fact_fingerprint
                    or not isinstance(closed, ContextWindowClosedEvent)
                    or closed.id != started.plan.stable_source_window_close_event_id
                    or closed.window_id != started.plan.source_window_id
                    or closed.window_generation != started.plan.source_window_generation
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
                    or opened.window.generation != started.plan.target_window_generation
                    or opened.window.source_compaction_id != terminal.compaction_id
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
                    decode_raw_stored_event_envelope(
                        item, DEFAULT_EVENT_SCHEMA_REGISTRY
                    )
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
                start = self.event_log.get_by_id(boundary.original_run_start_event_id)
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
                        decoded := decode_raw_stored_event_envelope(
                            item, DEFAULT_EVENT_SCHEMA_REGISTRY
                        ),
                        CapabilityExposureResolvedEvent,
                    )
                    and decoded.exposure.exposure_id == boundary.source_exposure_id
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

    def _validate_compaction_extension_batch(
        self,
        events: tuple[AgentEvent, ...],
    ) -> None:
        from pulsara_agent.primitives.compaction import (
            CompactionPostCompletionExtensionLinkFact,
            CompactionPostCompletionExtensionRequestedFact,
        )
        from pulsara_agent.primitives.frozen import build_frozen_fact

        extension_events = tuple(
            event
            for event in events
            if isinstance(
                event,
                (
                    ContextCompactionCompletedEvent,
                    ContextCompactionMemoryExtractionRequestedEvent,
                ),
            )
        )
        for index, event in enumerate(extension_events):
            if isinstance(event, ContextCompactionCompletedEvent):
                requested = tuple(
                    item
                    for item in event.post_completion_extension_dispositions
                    if isinstance(item, CompactionPostCompletionExtensionRequestedFact)
                )
                if not requested:
                    continue
                if len(requested) != 1 or index + 1 >= len(extension_events):
                    raise ValueError(
                        "compaction requested disposition requires one companion request"
                    )
                request = extension_events[index + 1]
                if not isinstance(
                    request, ContextCompactionMemoryExtractionRequestedEvent
                ):
                    raise ValueError(
                        "compaction request must immediately follow Completed"
                    )
                link = requested[0].extension_link
                if request.extension_link != link:
                    raise ValueError("compaction Completed/Request link drifted")
                expected = build_frozen_fact(
                    CompactionPostCompletionExtensionLinkFact,
                    schema_version="compaction_post_completion_extension_link.v1",
                    compaction_id=event.compaction_id,
                    completed_event_id=event.id,
                    request_event_id=request.id,
                    extension_contract_fingerprint=(
                        link.extension_contract_fingerprint
                    ),
                )
                if link != expected:
                    raise ValueError("compaction extension link identity is invalid")
                if (
                    request.run_id != event.run_id
                    or request.turn_id != event.turn_id
                    or request.reply_id != event.reply_id
                    or link.compaction_id != event.compaction_id
                ):
                    raise ValueError(
                        "compaction extension request occurrence does not join Completed"
                    )
                expected_occurrence = context_fingerprint(
                    "compaction-memory-extraction-request-occurrence:v1",
                    (
                        self.runtime_session_id,
                        event.compaction_id,
                        event.id,
                        request.id,
                    ),
                )
                if request.business_occurrence_fingerprint != expected_occurrence:
                    raise ValueError(
                        "compaction extraction request occurrence fingerprint drifted"
                    )
                expected_semantic = context_fingerprint(
                    "context-compaction-memory-extraction-request-semantic:v1",
                    {
                        "manifest_semantic": (
                            request.human_evidence_manifest_reference.manifest_semantic_fingerprint
                        ),
                        "memory_domain_id": request.memory_domain_id,
                        "resolved_scope": request.resolved_scope,
                        "extraction_contract": (
                            request.extraction_contract.contract_fingerprint
                        ),
                    },
                )
                if request.event_semantic_fingerprint != expected_semantic:
                    raise ValueError(
                        "compaction extraction request semantic fingerprint drifted"
                    )
            elif index == 0 or not isinstance(
                extension_events[index - 1], ContextCompactionCompletedEvent
            ):
                raise ValueError(
                    "compaction extraction Requested cannot be submitted independently"
                )

    @staticmethod
    def _validate_terminal_monitor_registration_batch(
        events: tuple[AgentEvent, ...],
    ) -> None:
        from pulsara_agent.event import TerminalProcessMonitorRegisteredEvent

        registrations = tuple(
            item
            for item in events
            if isinstance(item, TerminalProcessMonitorRegisteredEvent)
        )
        if not registrations:
            return
        ends = {
            item.id: item for item in events if isinstance(item, ToolResultEndEvent)
        }
        for registration in registrations:
            end = ends.get(registration.tool_result_end_event_id)
            if end is None:
                raise ValueError(
                    "terminal monitor registration requires same-batch ToolResultEnd"
                )
            semantic = end.terminal_process_monitor_registration
            if (
                end.tool_call_id
                != registration.registration_attribution.origin_tool_call_id
                or semantic is None
                or semantic != registration.registration_semantic
            ):
                raise ValueError(
                    "terminal monitor registration/tool terminal identity drifted"
                )

    def _validate_terminal_monitor_cancellation_batch(
        self,
        events: tuple[AgentEvent, ...],
    ) -> None:
        from pulsara_agent.event import (
            TerminalNotificationReservationReleasedEvent,
            TerminalProcessMonitorTerminatedEvent,
        )
        from pulsara_agent.llm.terminal_projection import stable_event_identity

        ends = tuple(
            item
            for item in events
            if isinstance(item, ToolResultEndEvent)
            and item.terminal_process_monitor_cancellation is not None
        )
        for end in ends:
            cancellation = end.terminal_process_monitor_cancellation
            assert cancellation is not None
            intent = cancellation.cancel_intent
            record = self.terminal_monitor_store.snapshot(intent.monitor_id)
            if (
                cancellation.expected_monitor_state_revision
                != record.core_state.state_revision
                or cancellation.expected_monitor_core_state_fingerprint
                != record.core_state.core_state_fingerprint
                or intent.tool_result_end_event_id != end.id
                or intent.origin_cancel_tool_call_id != end.tool_call_id
            ):
                raise ValueError("terminal monitor cancellation source CAS failed")
            terminations = tuple(
                item
                for item in events
                if isinstance(item, TerminalProcessMonitorTerminatedEvent)
                and item.id == intent.monitor_termination_event_id
            )
            if len(terminations) != 1:
                raise ValueError(
                    "terminal monitor cancellation requires one same-batch termination"
                )
            termination = terminations[0]
            if (
                termination.termination_semantic.monitor_id != intent.monitor_id
                or termination.termination_semantic.terminal_reason != "explicit_cancel"
            ):
                raise ValueError("terminal monitor cancellation termination drifted")
            releases = tuple(
                item
                for item in events
                if isinstance(item, TerminalNotificationReservationReleasedEvent)
                and item.transition.reservation.reservation_id
                == termination.notification_reservation_id
            )
            if len(releases) != 1:
                raise ValueError(
                    "terminal monitor cancellation requires one same-batch release"
                )
            termination_identity = stable_event_identity(
                termination,
                runtime_session_id=self.runtime_session_id,
            )
            if (
                termination_identity
                not in releases[0].transition.cause_event_identities
            ):
                raise ValueError("terminal monitor cancellation release cause drifted")

    def _validate_terminal_projection_batch(
        self,
        events: tuple[AgentEvent, ...],
    ) -> None:
        from pulsara_agent.llm.terminal_projection import (
            stable_event_identity,
            validate_model_terminal_projection_document,
        )

        projection_types = (
            ModelCallTerminalProjectionCommittedEvent,
            ToolResultTerminalProjectionCommittedEvent,
        )
        terminal_types = (ModelCallEndEvent, ToolResultEndEvent)
        for index, event in enumerate(events):
            if isinstance(event, projection_types):
                if (
                    isinstance(event, ToolResultTerminalProjectionCommittedEvent)
                    and event.source_kind == "external_requirement"
                ):
                    external_terminal = next(
                        (
                            item
                            for item in events[index + 1 :]
                            if not isinstance(item, projection_types)
                        ),
                        None,
                    )
                    if not isinstance(
                        external_terminal, ExternalExecutionResultEvent
                    ) or event.projection_reference not in tuple(
                        item.projection_reference
                        for item in external_terminal.terminal_projections
                    ):
                        raise ValueError(
                            "external terminal projection requires its result batch"
                        )
                    continue
                if index + 1 >= len(events) or not isinstance(
                    events[index + 1], terminal_types
                ):
                    raise ValueError(
                        "terminal projection must immediately precede its End"
                    )
                continue
            if not isinstance(event, terminal_types):
                if not isinstance(event, ExternalExecutionResultEvent):
                    continue
                projection_count = len(event.terminal_projections)
                if projection_count != len(event.external_results):
                    raise ValueError(
                        "external result requires one terminal projection per result"
                    )
                if index < projection_count:
                    raise ValueError(
                        "external result requires adjacent terminal projections"
                    )
                committed_projections = events[index - projection_count : index]
                if not all(
                    isinstance(item, ToolResultTerminalProjectionCommittedEvent)
                    and item.source_kind == "external_requirement"
                    for item in committed_projections
                ):
                    raise ValueError(
                        "external result requires adjacent external projections"
                    )
                requirements: dict[str, RequireExternalExecutionEvent] = {}
                ingress_by_call = {
                    item.result_block.tool_call_id: item
                    for item in event.external_results
                }
                for committed, end_reference in zip(
                    committed_projections,
                    event.terminal_projections,
                    strict=True,
                ):
                    assert isinstance(
                        committed, ToolResultTerminalProjectionCommittedEvent
                    )
                    if (
                        committed.projection_reference
                        != end_reference.projection_reference
                        or end_reference.projection_committed_event_identity
                        != stable_event_identity(
                            committed,
                            runtime_session_id=self.runtime_session_id,
                        )
                    ):
                        raise ValueError(
                            "external terminal projection carrier identity drifted"
                        )
                    source_id = committed.source_event_identity.event_id
                    requirement = requirements.get(source_id)
                    if requirement is None:
                        stored = self.event_log.get_by_id(source_id)
                        if not isinstance(stored, RequireExternalExecutionEvent):
                            raise ValueError(
                                "external terminal projection requirement is unavailable"
                            )
                        requirement = stored
                        requirements[source_id] = requirement
                    ingress = ingress_by_call.get(committed.tool_call_id)
                    if ingress is None:
                        raise ValueError(
                            "external terminal projection result identity drifted"
                        )
                    document = self.transcript_projection_document_registry.resolve(
                        committed.projection_reference
                    )
                    self.tool_terminal_projection_service.validate_external_document(
                        requirement=requirement,
                        result=event,
                        ingress=ingress,
                        document=document,
                    )
                continue
            projection = event.terminal_projection
            if index == 0 or not isinstance(events[index - 1], projection_types):
                raise ValueError("terminal End requires an adjacent projection")
            committed = events[index - 1]
            if projection.projection_reference != committed.projection_reference:
                raise ValueError("terminal projection reference drifted")
            expected_identity = stable_event_identity(
                committed,
                runtime_session_id=self.runtime_session_id,
            )
            if projection.projection_committed_event_identity != expected_identity:
                raise ValueError("terminal projection event identity drifted")
            document = self.transcript_projection_document_registry.resolve(
                committed.projection_reference
            )
            if isinstance(event, ModelCallEndEvent):
                if not isinstance(committed, ModelCallTerminalProjectionCommittedEvent):
                    raise ValueError("model End requires a model projection event")
                start = self.event_log.get_by_id(
                    committed.model_call_start_event_identity.event_id
                )
                if not isinstance(start, ModelCallStartEvent):
                    raise ValueError("model terminal projection Start is unavailable")
                validate_model_terminal_projection_document(
                    runtime_session_id=self.runtime_session_id,
                    start=start,
                    committed=committed,
                    end=event,
                    document=document,
                )
            else:
                if not isinstance(
                    committed, ToolResultTerminalProjectionCommittedEvent
                ):
                    raise ValueError("tool End requires a tool projection event")
                self.tool_terminal_projection_service.validate_tool_document(
                    terminal=event,
                    document=document,
                    batch_prefix=events[:index],
                )

    def _commit_reduce_enqueue(
        self,
        events: tuple[AgentEvent, ...],
        *,
        expected_last_sequence: int | None,
        await_delivery: bool,
        deadline_monotonic: float,
        enqueue_publication: bool = True,
        transaction_companion: RuntimeTransactionCompanion | None = None,
        publication_terminal_maintenance: bool = False,
    ) -> _WriteAttempt:
        if not events:
            result = EventWriteResult(
                committed_events=(),
                accounting_events=(),
                stored_batch_receipt=None,
                business_accounting_partition_fingerprint=(
                    business_accounting_partition_fingerprint(
                        receipt=None,
                        business_events=(),
                        accounting_events=(),
                    )
                ),
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
        with self._host_ingress_commit_guard(events), self.write_coordinator.lock:
            materialization_account = self.materialization_account_store.snapshot()
            if materialization_account is None:
                try:
                    self.runtime_projection_checkpoint_maintenance_service.assert_event_admission(
                        events
                    )
                except RuntimeProjectionCheckpointAdmissionBlocked as exc:
                    # An unaccounted compatibility/test batch has no physical
                    # companion builder.  Its frozen events are already the
                    # complete storage candidate and can be checked here.
                    raise PendingRuntimeEventWriteError(str(exc)) from exc
            hard_reconciliation = (
                self._ledger_reconciliation_required
                or self._context_input_reconciliation_required
                or self._memory_governance_reconciliation_required
                or self._mandatory_audit_reconciliation_required
                or self._reconciliation_required
            )
            publication_blocked = (
                self._publication_reconciliation_required
                and not publication_terminal_maintenance
            )
            if hard_reconciliation or publication_blocked:
                failed = tuple(
                    f"{item.reducer_id}: {item.last_error or 'reconciliation required'}"
                    for item in self._committed_reducers.values()
                    if item.reconciliation_required
                )
                raise EventReconciliationRequired(
                    "RuntimeSession ledger or committed reducer requires reconciliation"
                    + (f" ({'; '.join(failed)})" if failed else "")
                )
            if (
                materialization_account is not None
                and expected_last_sequence is not None
                and expected_last_sequence
                != materialization_account.ledger_through_sequence
            ):
                # A caller may prepare a batch from a reducer snapshot while another
                # writer advances the ledger. Reject that stale candidate before any
                # reducer validates its sequence-sensitive fingerprints.
                raise EventWriteConflict(
                    runtime_session_id=self.runtime_session_id,
                    expected_last_sequence=expected_last_sequence,
                    actual_last_sequence=(
                        materialization_account.ledger_through_sequence
                    ),
                    deadline_monotonic=deadline_monotonic,
                )
            self._validate_run_lifecycle_batch(events)
            self.long_horizon_state_store.validate_next_batch(events)
            if materialization_account is not None:
                return self._commit_accounted_one_shot_reduce_enqueue(
                    events,
                    expected_last_sequence=expected_last_sequence,
                    await_delivery=await_delivery,
                    deadline_monotonic=deadline_monotonic,
                    enqueue_publication=enqueue_publication,
                    transaction_companion=transaction_companion,
                )
            if transaction_companion is not None:
                raise EventReconciliationRequired(
                    "transaction companion requires a materialization account"
                )
            commit_deadline = _commit_phase_deadline(deadline_monotonic)
            try:
                stored_batch_receipt = self.event_log.commit_batch(
                    events,
                    expected_last_sequence=expected_last_sequence,
                    deadline_monotonic=commit_deadline,
                )
                committed = stored_batch_receipt.owned_stored_events
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
                        await_delivery=await_delivery,
                        enqueue_publication=enqueue_publication,
                    )
                self._catch_up_reducers(exc.actual_last_sequence)
                self._catch_up_publisher(
                    through_sequence=exc.actual_last_sequence,
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
            presentation_fold: LiveCommittedFoldResult | None = None
            for registration in self._committed_reducers.values():
                try:
                    fold_result = self._apply_live_receipt_to_reducer(
                        registration,
                        stored_batch_receipt,
                        catch_up_through_sequence=last_sequence,
                        deadline_monotonic=deadline_monotonic,
                    )
                    if registration.reducer_id == self._transcript_reducer_id:
                        if not isinstance(fold_result, LiveCommittedFoldResult):
                            raise TypeError(
                                "canonical transcript live ingress returned wrong carrier"
                            )
                        presentation_fold = fold_result
                except Exception as exc:
                    self._install_committed_reducer_repair(
                        registration,
                        target_through_sequence=last_sequence,
                        error=exc,
                    )
                    reducer_errors.append(
                        CommittedReducerError(
                            reducer_id=registration.reducer_id,
                            error_type=type(exc).__name__,
                            message=_bounded_error(exc),
                        )
                    )

            if presentation_fold is not None:
                self._offer_presentation_tap_noexcept(
                    stored_batch_receipt,
                    presentation_fold,
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
                            )
                            for event in publisher_events
                        ),
                        await_delivery=await_delivery,
                    )
            result = EventWriteResult(
                committed_events=committed,
                accounting_events=(),
                stored_batch_receipt=stored_batch_receipt,
                business_accounting_partition_fingerprint=(
                    business_accounting_partition_fingerprint(
                        receipt=stored_batch_receipt,
                        business_events=committed,
                        accounting_events=(),
                    )
                ),
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

    def _assert_runtime_projection_physical_batch_admission(
        self,
        events: tuple[AgentEvent, ...],
    ) -> None:
        """Reject an exact accounted batch before it reaches physical storage."""

        try:
            self.runtime_projection_checkpoint_maintenance_service.assert_event_admission(
                events
            )
        except RuntimeProjectionCheckpointAdmissionBlocked as exc:
            raise PendingRuntimeEventWriteError(str(exc)) from exc

    def _commit_accounted_one_shot_reduce_enqueue(
        self,
        events: tuple[AgentEvent, ...],
        *,
        expected_last_sequence: int | None,
        await_delivery: bool,
        deadline_monotonic: float,
        enqueue_publication: bool,
        transaction_companion: RuntimeTransactionCompanion | None = None,
    ) -> _WriteAttempt:
        """Commit an already-frozen finite ledger batch with typed accounting."""

        source = self.materialization_account_store.snapshot()
        if source is None:
            raise EventReconciliationRequired(
                "materialization account disappeared before one-shot commit"
            )
        if (
            expected_last_sequence is not None
            and expected_last_sequence != source.ledger_through_sequence
        ):
            raise EventWriteConflict(
                runtime_session_id=self.runtime_session_id,
                expected_last_sequence=expected_last_sequence,
                actual_last_sequence=source.ledger_through_sequence,
                deadline_monotonic=deadline_monotonic,
            )
        identity = context_fingerprint(
            "runtime-one-shot-physical-operation:v1",
            tuple((event.id, str(event.type)) for event in events),
        ).removeprefix("sha256:")
        first = events[0]
        operation_kind = _fixed_one_shot_operation_kind(events)
        if operation_kind is PhysicalOperationKind.EXTERNAL_EXECUTION:
            raise EventReconciliationRequired(
                "external execution requires a retained physical reservation"
            )
        try:
            burst_contract = self.authority_materialization_contracts.burst_registry.unique_binding_for_operation(
                operation_kind
            ).contract
            starts = tuple(
                event for event in events if isinstance(event, RunStartEvent)
            )
            if starts:
                if (
                    len(starts) != 1
                    or operation_kind is not PhysicalOperationKind.HOST_RUN_BOUNDARY
                ):
                    raise EventReconciliationRequired(
                        "RunStart requires one Host boundary consumer rotation"
                    )
                start = starts[0]
                seed = start.run_transcript_seed_reference
                committed = (
                    self.materialization_coordinator.commit_run_seed_consumer_rotation(
                        context=EventContext(
                            run_id=start.run_id,
                            turn_id=start.turn_id,
                            reply_id=start.reply_id,
                        ),
                        business_events=events,
                        run_start_event_id=start.id,
                        seed_semantic_fingerprint=(
                            start.run_transcript_seed_semantic.seed_semantic_fingerprint
                        ),
                        seed_reference_fingerprint=seed.reference_fingerprint,
                        seed_source_through_sequence=(
                            seed.source_ledger_through_sequence
                        ),
                        seed_source_ledger_continuity_accumulator=(
                            seed.source_ledger_continuity_accumulator
                        ),
                        reservation_id=f"one_shot:{identity}",
                        owner_id=f"one_shot:{identity}",
                        burst_contract=burst_contract,
                        deadline_monotonic=deadline_monotonic,
                        transaction_companion=transaction_companion,
                    )
                )
            else:
                committed = self.materialization_coordinator.commit_one_shot_operation(
                    context=EventContext(
                        run_id=first.run_id,
                        turn_id=first.turn_id,
                        reply_id=first.reply_id,
                    ),
                    business_events=events,
                    reservation_id=f"one_shot:{identity}",
                    owner_id=f"one_shot:{identity}",
                    burst_contract=burst_contract,
                    deadline_monotonic=deadline_monotonic,
                    transaction_companion=transaction_companion,
                )
        except Exception as exc:
            from pulsara_agent.runtime.authority_materialization import (
                MaterializationAccountCommitFailed,
                MaterializationAccountReconciliationRequired,
            )

            if isinstance(exc, MaterializationAccountReconciliationRequired):
                self._latch_ledger_reconciliation_required()
                raise EventCommitError(
                    "Event batch materialization confirmation is unknown",
                    commit_outcome="unknown",
                    deadline_monotonic=deadline_monotonic,
                ) from exc
            if isinstance(exc, MaterializationAccountCommitFailed):
                raise EventCommitError(
                    "Event batch materialization was not committed",
                    commit_outcome="none",
                    deadline_monotonic=deadline_monotonic,
                ) from exc
            raise
        full_attempt = self._reconcile_confirmed_attempt(
            committed.stored_batch_receipt,
            catch_up_through_sequence=_event_sequence(
                committed.stored_batch_receipt.owned_stored_events[-1]
            ),
            await_delivery=await_delivery,
            enqueue_publication=enqueue_publication,
        )
        by_id = {
            event.id: event
            for event in committed.stored_batch_receipt.owned_stored_events
        }
        business = tuple(by_id[event.id] for event in events)
        accounting = tuple(
            event
            for event in committed.stored_batch_receipt.owned_stored_events
            if event.id not in {item.id for item in business}
        )
        return _WriteAttempt(
            result=full_attempt.result.with_partition(
                business_events=business,
                accounting_events=accounting,
            ),
            delivery_futures=full_attempt.delivery_futures,
            published_events=full_attempt.published_events,
        )

    def _commit_genesis_reduce_enqueue(
        self,
        events: tuple[AgentEvent, ...],
        *,
        await_delivery: bool,
        deadline_monotonic: float,
    ) -> _WriteAttempt:
        """Commit the first business facts and canonical account genesis together."""

        with self._host_ingress_commit_guard(events), self.write_coordinator.lock:
            self.require_mutation_allowed()
            if self.materialization_account_store.snapshot() is not None:
                raise EventWriteConflict(
                    runtime_session_id=self.runtime_session_id,
                    expected_last_sequence=0,
                    actual_last_sequence=self.event_log.next_sequence() - 1,
                    deadline_monotonic=deadline_monotonic,
                )
            self._validate_run_lifecycle_batch(events)
            self.long_horizon_state_store.validate_next_batch(events)
            start = next(event for event in events if isinstance(event, RunStartEvent))
            profile = (
                "subagent_first_run"
                if start.run_entry_kind.value == "subagent_child"
                else "host_first_run"
            )
            from pulsara_agent.runtime.authority_materialization import (
                MaterializationAccountCommitFailed,
                MaterializationAccountReconciliationRequired,
            )

            try:
                committed = self.materialization_coordinator.bootstrap_genesis(
                    context=EventContext(
                        run_id=start.run_id,
                        turn_id=start.turn_id,
                        reply_id=start.reply_id,
                    ),
                    business_events=events,
                    genesis_profile=profile,
                    genesis_burst_contract=(
                        self.authority_materialization_contracts.burst_registry.unique_binding_for_operation(
                            PhysicalOperationKind.LEDGER_GENESIS
                        ).contract
                    ),
                    register_transcript_consumer=True,
                    deadline_monotonic=deadline_monotonic,
                )
            except MaterializationAccountReconciliationRequired as exc:
                self._latch_ledger_reconciliation_required()
                raise EventCommitError(
                    "Event batch commit confirmation failed: materialization account",
                    commit_outcome="unknown",
                    deadline_monotonic=deadline_monotonic,
                ) from exc
            except MaterializationAccountCommitFailed as exc:
                raise EventCommitError(
                    "Event batch commit failed: materialization account",
                    commit_outcome="none",
                    deadline_monotonic=deadline_monotonic,
                ) from exc
            stored = committed.stored_batch_receipt.owned_stored_events
            full_attempt = self._reconcile_confirmed_attempt(
                committed.stored_batch_receipt,
                catch_up_through_sequence=_event_sequence(stored[-1]),
                await_delivery=await_delivery,
            )
            by_id = {event.id: event for event in stored}
            business = tuple(by_id[event.id] for event in events)
            accounting = tuple(
                event
                for event in stored
                if event.id not in {item.id for item in business}
            )
            return _WriteAttempt(
                result=full_attempt.result.with_partition(
                    business_events=business,
                    accounting_events=accounting,
                ),
                delivery_futures=full_attempt.delivery_futures,
                published_events=full_attempt.published_events,
            )

    def _confirm_committed_batch(
        self,
        candidates: tuple[AgentEvent, ...],
        *,
        deadline_monotonic: float,
    ) -> tuple[StoredEventBatchCommitReceipt | None, int]:
        confirmation = self.event_log.confirm_stored_batch(
            candidates,
            deadline_monotonic=deadline_monotonic,
        )
        if confirmation.evidence is None:
            self._latch_ledger_reconciliation_required()
            raise EventReconciliationRequired(
                "Stored event candidate confirmation is unavailable"
            )
        high_water = confirmation.evidence.actual_last_sequence
        if confirmation.disposition == "none":
            return None, high_water
        if (
            confirmation.disposition != "full"
            or confirmation.confirmed_full_batch is None
        ):
            self._latch_ledger_reconciliation_required()
            raise EventReconciliationRequired(
                "Stored event candidate confirmation is partial or conflicting"
            )
        return confirmation.confirmed_full_batch.receipt, high_water

    def _latch_ledger_reconciliation_required(self) -> None:
        self._ledger_reconciliation_required = True

    def accept_authority_materialization_transition(
        self,
        committed: StoredEventBatchCommitReceipt,
    ) -> None:
        """Fold and enqueue a coordinator-owned batch after its durable FULL commit."""

        events = committed.owned_stored_events
        self._sync_physical_reservation_facts(events)
        self._reconcile_confirmed_attempt(
            committed,
            catch_up_through_sequence=_event_sequence(events[-1]),
            await_delivery=False,
        )

    def _sync_physical_reservation_facts(
        self,
        committed: tuple[AgentEvent, ...],
    ) -> None:
        """Mirror the durable active-reservation set after a coordinator CAS."""

        for event in committed:
            if not isinstance(event, PhysicalOperationReservationCreatedEvent):
                continue
            reservation = event.reservation
            key = (reservation.owner_kind, reservation.owner_id)
            existing = self._physical_reservation_facts.get(key)
            if existing is not None and existing != reservation:
                self._latch_ledger_reconciliation_required()
                raise EventReconciliationRequired(
                    "authority transition reservation identity is ambiguous"
                )
            self._physical_reservation_facts[key] = reservation
        account = self.materialization_account_store.snapshot()
        if account is None:
            return
        active_keys = {
            (item.owner_kind, item.owner_id) for item in account.active_reservations
        }
        missing = active_keys.difference(self._physical_reservation_facts)
        if missing:
            self._latch_ledger_reconciliation_required()
            raise EventReconciliationRequired(
                "authority transition lost a process-local reservation fact"
            )
        self._physical_reservation_facts = {
            key: reservation
            for key, reservation in self._physical_reservation_facts.items()
            if key in active_keys
        }

    def _reconcile_confirmed_attempt(
        self,
        stored_batch_receipt: StoredEventBatchCommitReceipt,
        *,
        catch_up_through_sequence: int,
        await_delivery: bool,
        enqueue_publication: bool = True,
    ) -> _WriteAttempt:
        committed = stored_batch_receipt.owned_stored_events
        target_sequence = max(
            _event_sequence(committed[-1]),
            catch_up_through_sequence,
        )
        self._catch_up_reducers(
            target_sequence,
            live_receipt=stored_batch_receipt,
        )
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
                        )
                        for event in publisher_events
                    ),
                    await_delivery=await_delivery,
                )
        result = EventWriteResult(
            committed_events=committed,
            accounting_events=(),
            stored_batch_receipt=stored_batch_receipt,
            business_accounting_partition_fingerprint=(
                business_accounting_partition_fingerprint(
                    receipt=stored_batch_receipt,
                    business_events=committed,
                    accounting_events=(),
                )
            ),
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

    def _catch_up_reducers(
        self,
        through_sequence: int,
        *,
        live_receipt: StoredEventBatchCommitReceipt | None = None,
        deadline_monotonic: float | None = None,
    ) -> None:
        presentation_fold: LiveCommittedFoldResult | None = None
        for registration in self._committed_reducers.values():
            if registration.through_sequence >= through_sequence:
                continue
            try:
                if live_receipt is not None:
                    fold_result = self._apply_live_receipt_to_reducer(
                        registration,
                        live_receipt,
                        catch_up_through_sequence=through_sequence,
                        deadline_monotonic=deadline_monotonic,
                    )
                    if registration.reducer_id == self._transcript_reducer_id:
                        if fold_result is not None and not isinstance(
                            fold_result, LiveCommittedFoldResult
                        ):
                            raise TypeError(
                                "canonical transcript live ingress returned wrong carrier"
                            )
                        presentation_fold = fold_result
                else:
                    self._fold_committed_reducer_range(
                        registration,
                        through_sequence=through_sequence,
                        source_kind="runtime_catch_up",
                        deadline_monotonic=deadline_monotonic,
                    )
            except Exception as exc:
                self._install_committed_reducer_repair(
                    registration,
                    target_through_sequence=through_sequence,
                    error=exc,
                )
        if live_receipt is not None and presentation_fold is not None:
            self._offer_presentation_tap_noexcept(
                live_receipt,
                presentation_fold,
            )

    def _offer_presentation_tap_noexcept(
        self,
        receipt: StoredEventBatchCommitReceipt,
        fold_result: LiveCommittedFoldResult,
    ) -> None:
        """Keep the observational tap outside the durable writer failure surface."""

        try:
            self.ui_committed_event_tap.offer_committed_nowait(receipt, fold_result)
            self._offer_operational_activities_noexcept(receipt)
        except BaseException:
            # The concrete tap already contains its own validation/diagnostics
            # boundary.  This second boundary protects durable commits from a
            # replaced or future tap implementation that violates that contract.
            return

    def _offer_operational_activities_noexcept(
        self, receipt: StoredEventBatchCommitReceipt
    ) -> None:
        """Project bounded in-flight hints without entering durable semantics."""

        store = self.ui_operational_activity_store
        for event in receipt.owned_stored_events:
            if isinstance(event, ModelCallStartEvent):
                call_id = event.resolved_call.resolved_model_call_id
                store.offer_nowait(
                    activity_kind="model_activity",
                    owner_kind="model_call",
                    owner_id=call_id,
                    owner_generation=1,
                    coalesce_key=f"model:{call_id}",
                    replacement_semantics="expire_at_terminal",
                    public_text="Model response in progress…",
                )
            elif isinstance(event, TextBlockSegmentEvent):
                call_id = event.model_stream_attribution.resolved_model_call_id
                store.offer_nowait(
                    activity_kind="model_activity",
                    owner_kind="model_call",
                    owner_id=call_id,
                    owner_generation=1,
                    coalesce_key=f"model:{call_id}",
                    replacement_semantics="expire_at_terminal",
                    public_text=event.text,
                )
            elif isinstance(event, ThinkingBlockSegmentEvent):
                call_id = event.model_stream_attribution.resolved_model_call_id
                store.offer_nowait(
                    activity_kind="model_activity",
                    owner_kind="model_call",
                    owner_id=call_id,
                    owner_generation=1,
                    coalesce_key=f"model:{call_id}",
                    replacement_semantics="expire_at_terminal",
                    public_text="Model reasoning in progress…",
                )
            elif isinstance(event, DataBlockSegmentEvent):
                call_id = event.model_stream_attribution.resolved_model_call_id
                store.offer_nowait(
                    activity_kind="model_activity",
                    owner_kind="model_call",
                    owner_id=call_id,
                    owner_generation=1,
                    coalesce_key=f"model:{call_id}",
                    replacement_semantics="expire_at_terminal",
                    public_text=f"Model data stream in progress ({event.media_type}).",
                )
            elif isinstance(event, ToolCallArgumentsSegmentEvent):
                call_id = event.model_stream_attribution.resolved_model_call_id
                store.offer_nowait(
                    activity_kind="model_activity",
                    owner_kind="model_call",
                    owner_id=call_id,
                    owner_generation=1,
                    coalesce_key=f"model:{call_id}",
                    replacement_semantics="expire_at_terminal",
                    public_text="Model is preparing a tool call…",
                )
            elif isinstance(event, ModelCallEndEvent):
                store.retire_nowait(
                    coalesce_key=f"model:{event.resolved_model_call_id}",
                    owner_kind="model_call",
                    owner_id=event.resolved_model_call_id,
                    owner_generation=1,
                    reason="durable_terminal",
                )
            elif isinstance(event, ToolResultStartEvent):
                store.offer_nowait(
                    activity_kind="tool_activity",
                    owner_kind="tool_call",
                    owner_id=event.tool_call_id,
                    owner_generation=1,
                    coalesce_key=f"tool:{event.tool_call_id}",
                    replacement_semantics="expire_at_terminal",
                    public_text=f"Running {event.tool_call_name}…",
                )
            elif isinstance(event, ToolResultEndEvent):
                store.retire_nowait(
                    coalesce_key=f"tool:{event.tool_call_id}",
                    owner_kind="tool_call",
                    owner_id=event.tool_call_id,
                    owner_generation=1,
                    reason="durable_terminal",
                )
            elif isinstance(event, TerminalProcessMonitorRegisteredEvent):
                monitor_id = event.registration_semantic.monitor_id
                process_id = event.registration_semantic.initial_baseline_cursor.stream_identity.process_id
                store.offer_nowait(
                    activity_kind="terminal_process_activity",
                    owner_kind="terminal_monitor",
                    owner_id=monitor_id,
                    owner_generation=1,
                    coalesce_key=f"terminal-monitor:{monitor_id}",
                    replacement_semantics="expire_at_terminal",
                    public_text=f"Monitoring terminal process {process_id}…",
                )
            elif isinstance(event, TerminalProcessMonitorObservationCommittedEvent):
                observation = event.observation
                monitor_id = observation.monitor_id
                if observation.observation_kind in {
                    "process_completed",
                    "monitor_expired",
                }:
                    store.retire_nowait(
                        coalesce_key=f"terminal-monitor:{monitor_id}",
                        owner_kind="terminal_monitor",
                        owner_id=monitor_id,
                        owner_generation=1,
                        reason="durable_terminal",
                    )
                else:
                    preview = observation.output_authority.output_preview
                    store.offer_nowait(
                        activity_kind="terminal_process_activity",
                        owner_kind="terminal_monitor",
                        owner_id=monitor_id,
                        owner_generation=1,
                        coalesce_key=f"terminal-monitor:{monitor_id}",
                        replacement_semantics="expire_at_terminal",
                        public_text=(
                            preview if preview else "Terminal process is still running…"
                        ),
                    )
            elif isinstance(event, TerminalProcessMonitorTerminatedEvent):
                monitor_id = event.termination_semantic.monitor_id
                store.retire_nowait(
                    coalesce_key=f"terminal-monitor:{monitor_id}",
                    owner_kind="terminal_monitor",
                    owner_id=monitor_id,
                    owner_generation=1,
                    reason="durable_terminal",
                )
            elif isinstance(event, SubagentRunStartedEvent):
                store.offer_nowait(
                    activity_kind="subagent_activity",
                    owner_kind="subagent_run",
                    owner_id=event.subagent_run_id,
                    owner_generation=1,
                    coalesce_key=f"subagent:{event.subagent_run_id}",
                    replacement_semantics="expire_at_terminal",
                    public_text=(f"Subagent {event.label or event.role} is running…"),
                )
            elif isinstance(event, SubagentPhaseReportedEvent):
                store.offer_nowait(
                    activity_kind="subagent_activity",
                    owner_kind="subagent_run",
                    owner_id=event.subagent_run_id,
                    owner_generation=1,
                    coalesce_key=f"subagent:{event.subagent_run_id}",
                    replacement_semantics="expire_at_terminal",
                    public_text=f"Subagent phase: {event.phase}.",
                )
            elif isinstance(event, SubagentRunSuspendedEvent):
                store.offer_nowait(
                    activity_kind="subagent_activity",
                    owner_kind="subagent_run",
                    owner_id=event.subagent_run_id,
                    owner_generation=1,
                    coalesce_key=f"subagent:{event.subagent_run_id}",
                    replacement_semantics="expire_at_terminal",
                    public_text=f"Subagent is suspended ({event.reason_code}).",
                )
            elif isinstance(
                event,
                (
                    SubagentRunCompletedEvent,
                    SubagentRunFailedEvent,
                    SubagentRunCancelledEvent,
                ),
            ):
                store.retire_nowait(
                    coalesce_key=f"subagent:{event.subagent_run_id}",
                    owner_kind="subagent_run",
                    owner_id=event.subagent_run_id,
                    owner_generation=1,
                    reason="durable_terminal",
                )

    def _catch_up_publisher(
        self,
        *,
        through_sequence: int,
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

    async def commit_accepted_event(self, event: AgentEvent) -> AgentEvent:
        """Commit one event under the closed Runtime acceptance policy.

        This is intentionally not named ``emit``: durability alone is not a
        successful production outcome.  The method returns the stored event
        only after the central classifier has accepted semantic settlement,
        checkpoint handoff where applicable, and required publication.
        """

        result = await self.write_event(event)
        self.accept_committed_event_result(
            result,
            requested_event_ids=(event.id,),
            require_publication=self._batch_requires_publication_confirmation((event,)),
        )
        return next(item for item in result.committed_events if item.id == event.id)

    async def commit_accepted_events(
        self,
        events: Iterable[AgentEvent],
        *,
        expected_last_sequence: int | None = None,
        transaction_companion: RuntimeTransactionCompanion | None = None,
    ) -> list[AgentEvent]:
        """Commit a batch under the closed Runtime acceptance policy."""

        candidates = tuple(events)
        result = await self.write_events(
            candidates,
            expected_last_sequence=expected_last_sequence,
            transaction_companion=transaction_companion,
        )
        requested_ids = tuple(event.id for event in candidates)
        self.accept_committed_event_result(
            result,
            requested_event_ids=requested_ids,
            require_publication=self._batch_requires_publication_confirmation(
                tuple(result.committed_events)
            ),
        )
        return list(result.committed_events)

    def accept_committed_event_result(
        self,
        result: EventWriteResult,
        *,
        requested_event_ids: tuple[str, ...],
        require_publication: bool,
        required_reducer_ids: tuple[str, ...] | None = None,
    ) -> CommittedEventSettlementReceipt:
        target = max(
            (
                int(event.sequence or 0)
                for event in result.committed_events
                if event.id in set(requested_event_ids)
            ),
            default=0,
        )
        required = (
            tuple(self._committed_reducers)
            if required_reducer_ids is None
            else tuple(dict.fromkeys(required_reducer_ids))
        )
        unknown = tuple(
            reducer_id
            for reducer_id in required
            if reducer_id not in self._committed_reducers
        )
        if unknown:
            raise EventReconciliationRequired(
                f"event settlement names unknown reducers: {unknown!r}"
            )
        checkpointed = tuple(
            registration.reducer_id
            for registration in self._committed_reducers.values()
            if registration.reducer_id in required
            if registration.last_fold_receipt is not None
            and registration.last_fold_receipt.resulting_through_sequence >= target
            and registration.last_fold_receipt.checkpoint_state is not None
        )
        return classify_committed_event_settlement(
            result,
            requested_event_ids=requested_event_ids,
            required_reducer_ids=required,
            repair_owner_installed=self.committed_reducer_repair_service.owns,
            checkpoint_handoff_accepted=(
                self.runtime_projection_checkpoint_maintenance_service.checkpoint_handoff_accepted
            ),
            checkpointed_reducer_ids=checkpointed,
            require_publication=require_publication,
        )

    def pending_committed_reducer_repair_handles(self):
        # Join the session writer boundary before observing registrations.  A
        # stored event can already be visible in EventLog while its committed
        # reducer callback is still installing the exact repair owner under
        # this lock.  Reading the flags without this join creates a false
        # healthy safe point in that narrow durable-FULL/handoff interval.
        with self.write_coordinator.lock:
            handles = []
            for registration in self._committed_reducers.values():
                if not registration.reconciliation_required:
                    continue
                handle = self.committed_reducer_repair_service.handle_for(
                    registration.reducer_id,
                    registration.through_sequence + 1,
                )
                if handle is None:
                    raise EventReconciliationRequired(
                        f"committed reducer {registration.reducer_id!r} has no online repair owner"
                    )
                handles.append(handle)
            return tuple(handles)

    async def await_committed_reducer_repair_safe_point(
        self,
        *,
        deadline_monotonic: float,
    ) -> tuple[object, ...]:
        """Wait for exact online semantic repairs before a run safe point.

        The repair tasks are session-owned.  Cancelling this coroutine only
        detaches its waiter; it cannot cancel or replace their stable plans.
        Once all matching receipts are installed, every other reconciliation
        latch is checked independently and still fails closed.
        """

        handles = self.pending_committed_reducer_repair_handles()
        receipts: list[object] = []
        for handle in handles:
            receipt = await self.wait_committed_reducer_repair(
                handle,
                deadline_monotonic=deadline_monotonic,
            )
            if (
                receipt.plan_fingerprint != handle.plan_fingerprint
                or receipt.reducer_id != handle.reducer_id
                or receipt.repaired_through_sequence != handle.target_ledger_high_water
            ):
                raise EventReconciliationRequired(
                    "committed reducer safe-point repair receipt is stale"
                )
            receipts.append(receipt)
        self.require_mutation_allowed()
        return tuple(receipts)

    async def drain_open_committed_reducer_barrier(
        self,
        *,
        deadline_monotonic: float,
    ) -> None:
        """Reach a semantic fixed point without closing repair admission.

        Host teardown uses this between producer cohorts.  A durable FULL may
        install an exact reducer repair and immediately latch subsequent
        ordinary writes; waiting only at the final close boundary would make
        the next terminalization producer fail before that boundary is
        reachable.
        """

        await self.event_write_service.drain_pending(
            deadline_monotonic=deadline_monotonic
        )
        await self.committed_reducer_repair_service.drain_pending(
            deadline_monotonic=deadline_monotonic
        )
        await self.committed_reducer_post_fold_service.drain_pending(
            deadline_monotonic=deadline_monotonic
        )

    async def wait_committed_reducer_repair(
        self,
        handle,
        *,
        deadline_monotonic: float,
    ):
        return await self.committed_reducer_repair_service.wait(
            handle,
            deadline_monotonic=deadline_monotonic,
        )

    def committed_reducer_operational_diagnostics(self) -> dict[str, object]:
        """Return bounded, secret-free live repair/checkpoint diagnostics."""

        with self.write_coordinator.lock:
            registrations = tuple(
                {
                    "reducer_id": registration.reducer_id,
                    "through_sequence": registration.through_sequence,
                    "reconciliation_required": (registration.reconciliation_required),
                    "last_error_code": (
                        None
                        if registration.last_error is None
                        else registration.last_error.split(":", 1)[0].upper()
                    ),
                }
                for registration in self._committed_reducers.values()
            )
            aggregate_reconciliation = self._reconciliation_required
        return {
            "reconciliation_required": aggregate_reconciliation,
            "registrations": registrations,
            "repair_attempts": (self.committed_reducer_repair_service.diagnostics()),
            "checkpoint_attempts": (
                self.runtime_projection_checkpoint_maintenance_service.all_diagnostics()
            ),
            "post_fold_attempts": (
                self.committed_reducer_post_fold_service.diagnostics()
            ),
        }

    def settle_event_from_thread(
        self, event: AgentEvent
    ) -> RuntimeThreadEventSettlementReceipt:
        batch = self.settle_events_from_thread((event,))
        return RuntimeThreadEventSettlementReceipt(
            committed_event=batch.committed_events[0],
            settlement=batch.settlement,
        )

    def settle_events_from_thread(
        self,
        events: Iterable[AgentEvent],
        *,
        required_reducer_ids: tuple[str, ...] | None = None,
        require_publication: bool | None = None,
        deadline_monotonic: float | None = None,
    ) -> RuntimeThreadEventBatchSettlementReceipt:
        """Commit and classify one thread-owned batch without losing outcome axes."""

        candidates = tuple(events)
        if not candidates:
            raise ValueError("thread event batch settlement cannot be empty")
        result = self.write_events_from_thread(
            candidates,
            deadline_monotonic=deadline_monotonic,
        )
        selected_reducer_ids = required_reducer_ids
        if len(candidates) == 1 and isinstance(
            candidates[0], TerminalProcessCompletedEvent
        ):
            # Completion lifecycle retirement depends on the notification
            # semantic authority, not on unrelated transcript/UI reducers.
            # A failure in another reducer remains visible on EventWriteResult
            # and the session latch, but can never make the physical process
            # owner replay an event that is already durable FULL.
            completion_reducer_ids = (
                f"terminal_notification:{self.runtime_session_id}",
            )
            if (
                selected_reducer_ids is not None
                and selected_reducer_ids != completion_reducer_ids
            ):
                raise ValueError(
                    "terminal completion thread settlement reducer policy drifted"
                )
            selected_reducer_ids = completion_reducer_ids
        settlement = self.accept_committed_event_result(
            result,
            requested_event_ids=tuple(event.id for event in candidates),
            require_publication=(
                self._batch_requires_critical_publication(candidates)
                if require_publication is None
                else require_publication
            ),
            required_reducer_ids=selected_reducer_ids,
        )
        stored_by_id = {event.id: event for event in result.committed_events}
        committed = tuple(stored_by_id[event.id] for event in candidates)
        return RuntimeThreadEventBatchSettlementReceipt(
            committed_events=committed,
            settlement=settlement,
        )

    def publish_stored_event(self, event: AgentEvent) -> None:
        if event.sequence is None:
            raise ValueError("Stored events must have a canonical sequence")
        self._require_default_metadata_present(event)
        self.publish_stored_events((event,))

    def publish_stored_events(
        self,
        events: Iterable[AgentEvent],
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
                    )
                    for stored in publish_events
                ),
                await_delivery=False,
            )

    def make_thread_recorder(self) -> RuntimeThreadRecorder:
        return RuntimeThreadRecorder(runtime_session=self)

    async def quiesce_provider_input_event_producers_for_close(
        self,
        *,
        deadline_monotonic: float,
    ) -> ProviderInputCloseQuiesceReceipt:
        """Own provider-input close I/O without blocking the Host event loop.

        The stable session task and the underlying ContextInputIoService handle
        outlive a cancelled waiter.  Every PostgreSQL read and every FIFO write
        receives the one Host close deadline; no nested operation may mint a
        fresh timeout.
        """

        receipt = self._provider_input_close_quiesce_receipt
        if receipt is not None:
            return receipt
        task = self._provider_input_close_quiesce_task
        if task is None:
            task = asyncio.create_task(
                self._execute_provider_input_close_quiesce(
                    deadline_monotonic=deadline_monotonic
                ),
                name=f"provider-input-close-quiesce:{self.runtime_session_id}",
            )
            self._provider_input_close_quiesce_task = task
        remaining = deadline_monotonic - monotonic()
        if remaining <= 0:
            raise TimeoutError(
                "provider-input close quiesce deadline exceeded; close is blocked"
            )
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
        except TimeoutError as exc:
            raise TimeoutError(
                "provider-input close quiesce deadline exceeded; close is blocked"
            ) from exc

    async def _execute_provider_input_close_quiesce(
        self,
        *,
        deadline_monotonic: float,
    ) -> ProviderInputCloseQuiesceReceipt:
        def physical_operation() -> ProviderInputCloseQuiesceReceipt:
            recovery_results = self.provider_input_preparation_recovery_service.recover_incomplete_preparations_sync(
                deadline_monotonic=deadline_monotonic
            )
            close_event_ids = self.provider_input_generation_coordinator.close_open_session_generations_sync(
                deadline_monotonic=deadline_monotonic
            )
            return ProviderInputCloseQuiesceReceipt(
                runtime_session_id=self.runtime_session_id,
                preparation_outcomes=tuple(
                    (
                        item.preparation_id,
                        item.outcome,
                        item.terminal_event_id,
                    )
                    for item in recovery_results
                ),
                generation_close_event_ids=close_event_ids,
            )

        handle = await self.context_input_io_service.start_owned(
            operation_name="provider-input-close-quiesce",
            operation=physical_operation,
            deadline_monotonic=deadline_monotonic,
        )
        receipt = await handle.wait_physical_completion()
        if receipt.runtime_session_id != self.runtime_session_id:
            raise RuntimeError("provider-input close quiesce receipt owner drifted")
        self._provider_input_close_quiesce_receipt = receipt
        return receipt

    def _confirm_provider_input_close_quiesced(self) -> None:
        """Accept only a finished receipt plus semantically folded close state."""

        task = self._provider_input_close_quiesce_task
        if task is not None:
            if not task.done():
                raise RuntimeError(
                    "cannot close RuntimeSession while provider-input quiesce is pending"
                )
            receipt = task.result()
            if receipt.runtime_session_id != self.runtime_session_id:
                raise RuntimeError("provider-input close quiesce receipt owner drifted")
            self._provider_input_close_quiesce_receipt = receipt
        active_preparations = (
            self.provider_input_generation_store.active_preparation_snapshots()
        )
        open_generations = (
            self.provider_input_generation_store.open_session_continuity_snapshots()
        )
        if active_preparations or open_generations:
            raise RuntimeError(
                "cannot close RuntimeSession before provider-input close authority "
                "is semantically folded"
            )
        # A direct RuntimeSession used by a focused test may have no provider
        # producers at all.  Its empty local state is a valid no-I/O receipt;
        # active state must always pass through the async Host-owned operation.
        if self._provider_input_close_quiesce_receipt is None:
            self._provider_input_close_quiesce_receipt = (
                ProviderInputCloseQuiesceReceipt(
                    runtime_session_id=self.runtime_session_id,
                    preparation_outcomes=(),
                    generation_close_event_ids=(),
                )
            )
        self.provider_input_generation_coordinator.close_owned_attempts_after_recovery()

    async def teardown_temporary_recovery_session(
        self,
        *,
        deadline_monotonic: float,
    ) -> None:
        """Drain a resume-recovery RuntimeSession before synchronous close.

        A temporary recovery session is a real durable writer: repairing a run
        may dirty runtime-projection and transcript checkpoint owners.  It has
        no HostSession to execute the normal teardown graph, so this narrow
        owner supplies the corresponding bounded subset.  No error is ignored
        and no owner is force-cleared.
        """

        await self.quiesce_provider_input_event_producers_for_close(
            deadline_monotonic=deadline_monotonic
        )
        await self.mandatory_runtime_audit_owner.drain(
            deadline_monotonic=deadline_monotonic
        )
        await self.drain_open_committed_reducer_barrier(
            deadline_monotonic=deadline_monotonic
        )
        self.require_mutation_allowed()

        # Runtime-projection maintenance consumes reducer fold receipts.  Stop
        # and physically drain it only after the writer/repair/post-fold fixed
        # point, while its dependencies are still alive.
        await self.runtime_projection_checkpoint_maintenance_service.stop_admission_and_drain(
            deadline_monotonic=deadline_monotonic
        )

        if self.window_compaction_service is not None:
            await self.window_compaction_service.drain_pending(
                deadline_monotonic=deadline_monotonic
            )
        await self.context_input_manifest_service.drain_pending(
            deadline_monotonic=deadline_monotonic
        )
        await self.transcript_projection_checkpoint_service.request_close_cancellation()
        await self.subagent_graph_checkpoint_service.drain_pending(
            deadline_monotonic=deadline_monotonic
        )
        await self.transcript_projection_checkpoint_service.drain_pending(
            deadline_monotonic=deadline_monotonic
        )
        await self.prompt_queue_checkpoint_service.drain_pending(
            deadline_monotonic=deadline_monotonic
        )
        await self.terminal_presentation_foundation_service.stop_admission_and_drain(
            deadline_monotonic=deadline_monotonic
        )
        # Every owner above can use the shared blocking I/O service.  This is
        # the final physical-operation join before close_if_idle() assertions.
        await self.context_input_io_service.drain_pending(
            deadline_monotonic=deadline_monotonic
        )
        self.close()

    def close(self) -> None:
        # Owned-local: we shut the manager down. Borrowed (HostCore path): we do
        # NOT kill/detach/shutdown the shared manager here — lease release is the
        # supervisor/HostCore job and must run exactly once (contract §5).
        # Idempotent: shutting an already-shut manager down is a no-op.
        if self.model_call_control_disposition_owner.pending_candidate_count:
            raise RuntimeError(
                "cannot close RuntimeSession with pending model control disposition"
            )
        if self._physical_operation_admission_tokens:
            raise RuntimeError(
                "cannot close RuntimeSession with active physical operation admissions"
            )
        self._confirm_provider_input_close_quiesced()
        self.terminal_presentation_foundation_service.close()
        self.terminal_monitor_coordinator.close()
        self._terminal_notification_listener = None
        self.context_input_manifest_service.close_if_idle()
        self.context_input_io_service.close_if_idle()
        self.event_write_service.close_if_idle()
        self.committed_reducer_post_fold_service.close_if_idle()
        self.committed_reducer_repair_service.close_if_idle()
        self.runtime_projection_checkpoint_maintenance_service.close_if_idle()
        self.subagent_graph_checkpoint_service.close_if_idle()
        self.transcript_projection_checkpoint_service.close_if_idle()
        self.prompt_queue_checkpoint_service.close_if_idle()
        if self.window_compaction_service is not None:
            self.window_compaction_service.close_if_idle()
        self.context_static_instruction_cache.clear()
        self.context_candidate_lifecycle_cache.clear()
        self.tool_result_render_cache.clear()
        self.provider_input_generation_store.clear_resident_cache()
        self.model_call_control_disposition_owner.clear()
        self.mandatory_runtime_audit_owner.close_if_idle()
        self._context_input_cache_diagnostics.clear()
        with self.write_coordinator.lock:
            self._publication_maintenance_coordinator.invalidate_issued()
            self._committed_reducers.clear()
            self._reconciliation_required = False
            self._ledger_reconciliation_required = False
            self._context_input_reconciliation_required = False
            self._memory_governance_reconciliation_required = False
            self._publication_reconciliation_required = False
            self._mandatory_audit_reconciliation_required = False
        if self._owns_terminal_manager:
            self.terminal_sessions.shutdown()


def _repair_post_fold_events(
    events: tuple[AgentEvent, ...],
    *,
    failed_registration_high_water: int,
) -> tuple[AgentEvent, ...]:
    """Return only control handoffs not published before the failed fold.

    The semantic rebuild may start at an older checkpoint and therefore needs
    its complete bounded delta.  Post-fold process owners and Host listeners,
    however, already consumed every event through the registration high-water;
    replaying that prefix could schedule duplicate autonomous ingress.
    """

    return tuple(
        event
        for event in events
        if int(event.sequence or 0) > failed_registration_high_water
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


_CHILD_PARENT_FIXED_EVENT_TYPES = frozenset(
    {
        EventType.SUBAGENT_RUN_STARTED,
        EventType.SUBAGENT_MESSAGE_SENT,
        EventType.SUBAGENT_RUN_SUSPENDED,
        EventType.SUBAGENT_RUN_COMPLETED,
        EventType.SUBAGENT_RUN_FAILED,
        EventType.SUBAGENT_RUN_CANCELLED,
        EventType.SUBAGENT_EDGE_RECORDED,
        EventType.SUBAGENT_RESULT_DELIVERED,
        EventType.SUBAGENT_TASK_CREATED,
        EventType.SUBAGENT_TASK_SCHEDULED,
        EventType.SUBAGENT_TASK_STARTED,
        EventType.SUBAGENT_TASK_BLOCKED,
        EventType.SUBAGENT_TASK_COMPLETED,
        EventType.SUBAGENT_TASK_FAILED,
        EventType.SUBAGENT_TASK_CANCELLED,
        EventType.SUBAGENT_PHASE_REPORTED,
        EventType.SUBAGENT_RESULT_SUBMITTED,
        EventType.SUBAGENT_RESULT_CONSUMED,
        EventType.SUBAGENT_ROLLOUT_BUDGET_RESOLVED,
        EventType.ROLLOUT_BUDGET_RESERVATION_CREATED,
        EventType.ROLLOUT_BUDGET_RESERVATION_SETTLED,
        EventType.CHILD_ROLLOUT_SUBACCOUNT_CLOSED,
    }
)
_HOST_BOUNDARY_FIXED_EVENT_TYPES = frozenset(
    {
        EventType.RUN_START,
        EventType.RUN_END,
        EventType.RUN_INTERACTION_RESUME_BOUNDARY,
        EventType.CAPABILITY_EXPOSURE_RESOLVED,
        EventType.MCP_CAPABILITY_SNAPSHOT_INSTALLED,
        EventType.CONTEXT_WINDOW_OPENED,
        EventType.CONTEXT_WINDOW_CLOSED,
        EventType.ROLLOUT_BUDGET_ACCOUNT_OPENED,
        EventType.ROLLOUT_BUDGET_ACCOUNT_CLOSED,
        EventType.TERMINAL_PROCESS_OBSERVATION_DELIVERY_DISPOSITION,
        EventType.TERMINAL_NOTIFICATION_RESERVATION_RELEASED,
        EventType.PROMPT_QUEUE_COMMITTED_TO_RUN,
    }
)
_EXTERNAL_EXECUTION_FIXED_EVENT_TYPES = frozenset(
    {
        EventType.REQUIRE_EXTERNAL_EXECUTION,
        EventType.EXTERNAL_EXECUTION_RESULT,
    }
)


def _fixed_one_shot_operation_kind(
    events: tuple[AgentEvent, ...],
) -> PhysicalOperationKind:
    event_types = frozenset(event.type for event in events)
    if event_types and event_types <= _CHILD_PARENT_FIXED_EVENT_TYPES:
        return PhysicalOperationKind.CHILD_PARENT_GRAPH_WRITE
    if event_types and event_types <= _HOST_BOUNDARY_FIXED_EVENT_TYPES:
        return PhysicalOperationKind.HOST_RUN_BOUNDARY
    if event_types & _EXTERNAL_EXECUTION_FIXED_EVENT_TYPES:
        return PhysicalOperationKind.EXTERNAL_EXECUTION
    return PhysicalOperationKind.RUNTIME_INTERNAL_WRITE


def _physical_operation_admission_owner_id(
    key: tuple[PhysicalOperationKind, str],
) -> str:
    operation_kind, owner_id = key
    return f"{operation_kind.value}:{owner_id}"


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
