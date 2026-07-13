"""Runtime session ownership for one active Pulsara backend run."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Literal
from uuid import uuid4

from pulsara_agent.event import (
    AgentEvent,
    CapabilityExposureResolvedEvent,
    ContextCompactionCompletedEvent,
    ContextCompactionFailedEvent,
    ContextCompactionStartedEvent,
    McpCapabilitySnapshotInstalledEvent,
    RunEndEvent,
    RunInteractionResumeBoundaryEvent,
    RunStartEvent,
)
from pulsara_agent.event_log import (
    EventBatchConfirmation,
    EventIdConflict,
    EventLog,
    EventLogWriteConflict,
)
from pulsara_agent.memory.candidates.proposal_sink import MemoryProposalSink
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.primitives.context import ContextStaticInstructionFact
from pulsara_agent.runtime.hooks import RuntimeHookManager
from pulsara_agent.runtime.context_input.candidate import InMemoryContextLifecycleCache
from pulsara_agent.runtime.context_input.manifest import (
    ContextInputManifestWriteService,
)
from pulsara_agent.runtime.context_input.io_service import ContextInputIoService
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


class EventCommitError(RuntimeError):
    """Event batch was not durably committed."""


class EventWriteConflict(RuntimeError):
    def __init__(
        self,
        *,
        runtime_session_id: str,
        expected_last_sequence: int,
        actual_last_sequence: int,
    ) -> None:
        self.runtime_session_id = runtime_session_id
        self.expected_last_sequence = expected_last_sequence
        self.actual_last_sequence = actual_last_sequence
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


class PublisherSequenceGapError(RuntimeError):
    """Committed publisher catch-up interval is unavailable or non-contiguous."""


@dataclass(slots=True)
class _CommittedReducerRegistration:
    reducer_id: str
    through_sequence: int
    apply_committed: Callable[[tuple[AgentEvent, ...]], None]
    rebuild_committed: Callable[[tuple[AgentEvent, ...]], None] | None = None
    reconciliation_required: bool = False


@dataclass(frozen=True, slots=True)
class _WriteAttempt:
    result: EventWriteResult
    delivery_futures: tuple[asyncio.Future[None], ...]
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
    mcp_supervisor: Any | None = None
    context_event_log_locator: Any | None = None
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
    context_static_instruction_cache: dict[
        tuple[str, str, str], ContextStaticInstructionFact
    ] = field(default_factory=dict, init=False, repr=False)
    context_candidate_lifecycle_cache: InMemoryContextLifecycleCache = field(
        init=False,
        repr=False,
    )
    tool_result_render_cache: InMemoryToolResultRenderCache = field(
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
        self.context_candidate_lifecycle_cache = InMemoryContextLifecycleCache()
        self.tool_result_render_cache = InMemoryToolResultRenderCache()
        self.publisher = RuntimeEventPublisher(
            runtime_session_id=self.runtime_session_id,
            next_sequence_to_publish=_next_publish_sequence(self.event_log),
        )
        self.publisher.subscribe(self.hook_manager)
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

        prepared = self._prepare_event_batch(candidates)
        with self.write_coordinator.lock:
            confirmation = self.event_log.confirm_batch(prepared)
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
                self._reconciliation_required = True
                raise
            registration.reconciliation_required = False
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
        attempt = self._commit_reduce_enqueue(
            prepared,
            expected_last_sequence=expected_last_sequence,
            state=state,
            await_delivery=True,
        )
        publication_errors = await _await_publication(
            attempt.published_events,
            attempt.delivery_futures,
        )
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
        return self._commit_reduce_enqueue(
            prepared,
            expected_last_sequence=expected_last_sequence,
            state=state,
            await_delivery=False,
        ).result

    def _prepare_event_batch(
        self, events: Sequence[AgentEvent]
    ) -> tuple[AgentEvent, ...]:
        prepared: list[AgentEvent] = []
        for event in events:
            self._require_runtime_managed_sequence(event)
            prepared.append(self._with_default_metadata(event))
        return tuple(prepared)

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
            existing = tuple(self.event_log.iter(run_id=terminal.run_id))
            starts = [event for event in existing if isinstance(event, RunStartEvent)]
            candidate_start = starts_in_batch.get(terminal.run_id)
            if candidate_start is not None:
                matching = next(
                    (event for event in starts if event.id == candidate_start.id),
                    None,
                )
                if matching is None:
                    starts.append(candidate_start)
                elif not _same_event_candidate(matching, candidate_start):
                    raise ValueError("RunStart retry payload conflicts with ledger")
            if len(starts) != 1:
                raise ValueError("RunEnd requires exactly one durable RunStart")
            if terminal.id != starts[0].terminal_run_end_event_id:
                raise ValueError("RunEnd id does not match RunStart terminal contract")
            existing_ends = [
                event for event in existing if isinstance(event, RunEndEvent)
            ]
            if existing_ends:
                if len(existing_ends) != 1 or not _same_event_candidate(
                    existing_ends[0], terminal
                ):
                    raise ValueError("run already has a conflicting durable RunEnd")

        compaction_starts = {
            event.id: event
            for event in events
            if isinstance(event, ContextCompactionStartedEvent)
        }
        existing_events = tuple(self.event_log.iter())
        for terminal in (
            event
            for event in events
            if isinstance(
                event,
                (ContextCompactionCompletedEvent, ContextCompactionFailedEvent),
            )
            and event.started_event_id is not None
        ):
            starts = [
                event
                for event in existing_events
                if isinstance(event, ContextCompactionStartedEvent)
                and event.id == terminal.started_event_id
            ]
            candidate_start = compaction_starts.get(terminal.started_event_id)
            if candidate_start is not None:
                starts.append(candidate_start)
            if len(starts) != 1:
                raise ValueError(
                    "compaction terminal requires exactly one matching Started"
                )
            started = starts[0]
            if (
                terminal.id != started.terminal_event_id
                or terminal.compaction_id != started.compaction_id
                or terminal.host_boundary_id != started.host_boundary_id
                or terminal.host_boundary_kind != started.host_boundary_kind
            ):
                raise ValueError("compaction terminal pairing contract mismatch")
            existing_terminals = [
                event
                for event in existing_events
                if isinstance(
                    event,
                    (ContextCompactionCompletedEvent, ContextCompactionFailedEvent),
                )
                and event.started_event_id == started.id
            ]
            if existing_terminals and not (
                len(existing_terminals) == 1
                and _same_event_candidate(existing_terminals[0], terminal)
            ):
                raise ValueError("compaction Started already has a terminal fact")

        existing_by_id = {event.id: event for event in existing_events}
        candidate_index = {event.id: index for index, event in enumerate(events)}
        exposure_candidates = {
            event.exposure.exposure_id: event
            for event in events
            if isinstance(event, CapabilityExposureResolvedEvent)
        }
        for boundary_event in (
            event
            for event in events
            if isinstance(event, RunInteractionResumeBoundaryEvent)
        ):
            boundary = boundary_event.boundary
            start = existing_by_id.get(boundary.original_run_start_event_id)
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
            source_exposure = next(
                (
                    event
                    for event in existing_events
                    if isinstance(event, CapabilityExposureResolvedEvent)
                    and event.run_id == boundary_event.run_id
                    and event.exposure.exposure_id == boundary.source_exposure_id
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
                raise EventReconciliationRequired(
                    "RuntimeSession ledger or committed reducer requires reconciliation"
                )
            self._validate_run_lifecycle_batch(events)
            try:
                committed = tuple(
                    self.event_log.extend(
                        events,
                        expected_last_sequence=expected_last_sequence,
                    )
                )
            except EventLogWriteConflict as exc:
                confirmed, confirmed_high_water = self._confirm_committed_batch(events)
                if confirmed is not None:
                    return self._reconcile_confirmed_attempt(
                        confirmed,
                        catch_up_through_sequence=max(
                            confirmed_high_water,
                            exc.actual_last_sequence,
                        ),
                        state=state,
                        await_delivery=await_delivery,
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
                ) from exc
            except Exception as exc:
                try:
                    confirmed, confirmed_high_water = self._confirm_committed_batch(
                        events
                    )
                except (EventIdConflict, EventReconciliationRequired):
                    raise
                except Exception as confirmation_error:
                    raise EventCommitError(
                        f"Event batch commit confirmation failed: {type(confirmation_error).__name__}"
                    ) from confirmation_error
                if confirmed is not None:
                    return self._reconcile_confirmed_attempt(
                        confirmed,
                        catch_up_through_sequence=confirmed_high_water,
                        state=state,
                        await_delivery=await_delivery,
                    )
                raise EventCommitError(
                    f"Event batch commit failed: {type(exc).__name__}"
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
                    self._reconciliation_required = True
                    reducer_errors.append(
                        CommittedReducerError(
                            reducer_id=registration.reducer_id,
                            error_type=type(exc).__name__,
                            message=_bounded_error(exc),
                        )
                    )

            publication_errors: tuple[EventPublicationError, ...] = ()
            try:
                publisher_events = self._publisher_catch_up_events(
                    current=committed,
                    first_sequence=first_sequence,
                )
            except EventReconciliationRequired as exc:
                publisher_events = ()
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
    ) -> tuple[tuple[AgentEvent, ...] | None, int]:
        confirmation = self.event_log.confirm_batch(candidates)
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
        if publisher_start > target_sequence:
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
    futures: tuple[asyncio.Future[None], ...],
) -> tuple[EventPublicationError, ...]:
    if not futures:
        return ()
    outcomes = await asyncio.gather(*futures, return_exceptions=True)
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
