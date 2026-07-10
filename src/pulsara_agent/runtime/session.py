"""Runtime session ownership for one active Pulsara backend run."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Literal
from uuid import uuid4

from pulsara_agent.event import AgentEvent
from pulsara_agent.event_log import (
    EventIdConflict,
    EventLog,
    EventLogWriteConflict,
)
from pulsara_agent.memory.candidates.proposal_sink import MemoryProposalSink
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.runtime.hooks import RuntimeHookManager
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
from pulsara_agent.runtime.tool_artifacts import ToolResultArtifactIndex, ToolResultArtifactService
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
        reducer_failed = any(error.reducer_id == reducer_id for error in self.reducer_errors)
        if reducer_sequence is None or reducer_sequence < last_sequence or reducer_failed:
            raise EventReconciliationRequired(
                f"Committed reducer {reducer_id!r} did not apply through sequence {last_sequence}"
            )
        return self.committed_events


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
    default_event_metadata: dict[str, Any] = field(default_factory=dict)
    publisher: RuntimeEventPublisher = field(init=False)
    write_coordinator: SessionWriteCoordinator = field(
        default_factory=SessionWriteCoordinator,
        init=False,
    )
    terminal_sessions: TerminalSessionManager = field(init=False)
    artifact_service: ToolResultArtifactService = field(init=False)
    _owns_terminal_manager: bool = field(default=False, init=False, repr=False)
    _terminal_owner: TerminalOwnerContext | None = field(default=None, init=False, repr=False)
    _committed_reducers: dict[str, _CommittedReducerRegistration] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _reconciliation_required: bool = field(default=False, init=False, repr=False)
    _ledger_reconciliation_required: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self.workspace_root = self.workspace_root.expanduser().resolve()
        self.artifact_service = ToolResultArtifactService(
            archive=self.archive,
            index=self.tool_result_artifacts,
            runtime_session_id=self.runtime_session_id,
        )
        self.publisher = RuntimeEventPublisher(
            runtime_session_id=self.runtime_session_id,
            next_sequence_to_publish=_next_publish_sequence(self.event_log),
        )
        self.publisher.subscribe(self.hook_manager)
        self._bind_terminal(self.terminal_binding)

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
            self.terminal_sessions = binding.manager or TerminalSessionManager(self.workspace_root)
            self._owns_terminal_manager = True
            self._terminal_owner = None

    @property
    def terminal_owner_host_session_id(self) -> str | None:
        return self._terminal_owner.host_session_id if self._terminal_owner is not None else None

    @property
    def terminal_owner_conversation_id(self) -> str | None:
        return self._terminal_owner.conversation_id if self._terminal_owner is not None else None

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
            key for key, value in self.default_event_metadata.items()
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
        return self._ledger_reconciliation_required or self._reconciliation_required

    @property
    def ledger_reconciliation_required(self) -> bool:
        return self._ledger_reconciliation_required

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
                registration.through_sequence = (
                    events[-1].sequence if events else 0
                )  # type: ignore[assignment]
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
            publication_errors=(*attempt.result.publication_errors, *publication_errors),
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

    def _prepare_event_batch(self, events: Sequence[AgentEvent]) -> tuple[AgentEvent, ...]:
        prepared: list[AgentEvent] = []
        for event in events:
            self._require_runtime_managed_sequence(event)
            prepared.append(self._with_default_metadata(event))
        return tuple(prepared)

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
            return _WriteAttempt(result=result, delivery_futures=(), published_events=())
        with self.write_coordinator.lock:
            if self.reconciliation_required:
                raise EventReconciliationRequired(
                    "RuntimeSession ledger or committed reducer requires reconciliation"
                )
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
                    confirmed, confirmed_high_water = self._confirm_committed_batch(events)
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
                raise EventCommitError(f"Event batch commit failed: {type(exc).__name__}") from exc

            first_sequence = _event_sequence(committed[0])
            last_sequence = _event_sequence(committed[-1])
            reducer_errors: list[CommittedReducerError] = []
            for registration in self._committed_reducers.values():
                try:
                    if registration.through_sequence < first_sequence - 1:
                        missing = _contiguous_interval(
                            self.event_log.iter(after_sequence=registration.through_sequence),
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

    async def emit(self, event: AgentEvent, *, state: LoopState | None = None) -> AgentEvent:
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

    def emit_from_thread(self, event: AgentEvent, *, state: LoopState | None = None) -> AgentEvent:
        result = self.write_events_from_thread((event,), state=state)
        return result.committed_events[0]

    def publish_stored_event(self, event: AgentEvent, *, state: LoopState | None = None) -> None:
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

    def make_thread_recorder(self, *, state: LoopState | None = None) -> RuntimeThreadRecorder:
        return RuntimeThreadRecorder(runtime_session=self, state=state)

    def close(self) -> None:
        # Owned-local: we shut the manager down. Borrowed (HostCore path): we do
        # NOT kill/detach/shutdown the shared manager here — lease release is the
        # supervisor/HostCore job and must run exactly once (contract §5).
        # Idempotent: shutting an already-shut manager down is a no-op.
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

        if record_event is not None and not isinstance(record_event, RuntimeThreadRecorder):
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


def _merge_event_metadata(default_metadata: dict[str, Any], event_metadata: dict[str, Any]) -> dict[str, Any]:
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


def _contiguous_interval(
    events: Iterable[AgentEvent],
    *,
    start: int,
    end: int,
) -> tuple[AgentEvent, ...]:
    if end < start:
        return ()
    selected = tuple(
        event
        for event in events
        if start <= _event_sequence(event) <= end
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
