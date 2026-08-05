"""Renderer-neutral terminal application service composition."""

from __future__ import annotations

import asyncio
from concurrent.futures import Executor
from dataclasses import asdict, dataclass, field, replace
from functools import partial
from threading import RLock
from time import monotonic
from typing import Awaitable, Callable

from pulsara_agent.event import EventContext
from pulsara_agent.blocking_executor import auxiliary_io_executor
from pulsara_agent.ports.terminal_application import (
    ApprovalRequestView,
    CancelMcpInteractionRequest,
    CloseSessionRequest,
    ControllerTakeoverRequest,
    DetachSessionRequest,
    McpInputRequestPublicView,
    McpInteractionView,
    PlanExitView,
    PlanQuestionView,
    PromptQueueItemView,
    QueueCancelRequest,
    ResolveApprovalRequest,
    ResolveMcpInteractionRequest,
    ResolvePlanExitRequest,
    ResolvePlanQuestionRequest,
    StartSuccessorSessionRequest,
    StopRunRequest,
    SubmitPromptRequest,
    TerminalCommandBinding,
    TerminalCommandOutcome,
    TerminalMutationRequest,
    TerminalUiSessionSnapshot,
    terminal_command_outcome_fingerprint,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.runtime.approval import ApprovalResolution, ToolApprovalDecision
from pulsara_agent.runtime.plan import (
    McpInputRequiredInteractionResolution,
    PendingApproval,
    PendingMcpInputRequired,
    PendingPlanInteraction,
    PlanExitResolution,
    PlanQuestionResolution,
)
from pulsara_agent.runtime.recovery import AbortKind
from pulsara_agent.runtime.terminal_application.attachment import (
    TerminalAttachmentRegistry,
)
from pulsara_agent.runtime.terminal_application.command_receipt import (
    TerminalCommandReceiptStorage,
)
from pulsara_agent.runtime.terminal_application.control_projection import (
    ControlProjectionCursor,
    ControlProjectionRead,
    TerminalControlProjectionStore,
    TerminalControlSourceCaptureOwner,
    TerminalProjectionSnapshotBundle,
    build_terminal_control_capture_input,
)
from pulsara_agent.runtime.terminal_application.prompt_queue import (
    CLIENT_VISIBLE_ACTIVE_QUEUE_STATES,
    PromptQueueSubmitRequest,
    prompt_queue_item_public_view_payload,
)
from pulsara_agent.runtime.terminal_application.secret import TerminalMcpSecretService
from pulsara_agent.runtime.terminal_presentation.service import (
    PresentationHistoryAdmissionRejected,
)
from pulsara_agent.runtime.terminal_presentation.public_text import (
    bounded_terminal_safe_public_text,
)
from pulsara_agent.runtime.terminal_presentation.viewport import (
    fit_viewport_snapshot_resident_suffix,
)


def terminal_request_semantic_fingerprint(request: TerminalMutationRequest) -> str:
    payload = asdict(request)
    binding = dict(payload.pop("binding"))
    binding.pop("request_semantic_fingerprint", None)
    payload.pop("request_fingerprint", None)
    return context_fingerprint(
        f"terminal-command-request:{request.command_kind}:v1",
        {"binding": binding, "payload": payload},
    )


@dataclass(slots=True)
class _CommandRecord:
    semantic_fingerprint: str
    outcome: TerminalCommandOutcome
    task: asyncio.Task[TerminalCommandOutcome] | None = None
    terminal_outcome: TerminalCommandOutcome | None = None


class TerminalCommandOwner:
    """Process owner for stable command execution and reconnect query."""

    def __init__(
        self,
        *,
        runtime_session_id: str,
        receipt_storage: TerminalCommandReceiptStorage,
        executor: Executor | None = None,
        operation_timeout_seconds: float = 30.0,
        maximum_records: int = 1024,
    ) -> None:
        self.runtime_session_id = runtime_session_id
        self.receipt_storage = receipt_storage
        self.executor = executor or auxiliary_io_executor()
        self.operation_timeout_seconds = operation_timeout_seconds
        self.maximum_records = maximum_records
        self._lock = RLock()
        self._admission_lock = asyncio.Lock()
        self._records: dict[tuple[str, str], _CommandRecord] = {}
        self._order: list[tuple[str, str]] = []
        self._installation_tasks: set[
            asyncio.Task[
                tuple[_CommandRecord, asyncio.Task[TerminalCommandOutcome] | None]
            ]
        ] = set()
        self._physical_storage_operations: set[asyncio.Future[object]] = set()
        self._closed = False

    async def query(
        self, *, client_instance_id: str, command_id: str
    ) -> TerminalCommandOutcome | None:
        with self._lock:
            record = self._records.get((client_instance_id, command_id))
            if record is not None:
                return record.outcome
        receipt = await self._run_storage(
            self.receipt_storage.query,
            runtime_session_id=self.runtime_session_id,
            client_instance_id=client_instance_id,
            command_id=command_id,
        )
        return None if receipt is None else receipt.outcome

    async def recover_pending(self, *, deadline_monotonic: float) -> None:
        """Close restart-orphaned admissions without replaying domain side effects."""

        receipts = await self._run_storage(
            self.receipt_storage.list_pending,
            absolute_deadline_monotonic=deadline_monotonic,
            runtime_session_id=self.runtime_session_id,
            maximum_items=self.maximum_records + 1,
        )
        if len(receipts) > self.maximum_records:
            raise RuntimeError("terminal pending-command recovery exceeds its bound")
        for receipt in receipts:
            key = (receipt.client_instance_id, receipt.command_id)
            with self._lock:
                existing = self._records.get(key)
                if existing is not None:
                    if (
                        existing.semantic_fingerprint
                        != receipt.request_semantic_fingerprint
                    ):
                        raise ValueError(
                            "terminal recovered command identity conflicts"
                        )
                    continue
            recovery = _replace_outcome(
                receipt.outcome,
                status="reconciliation_required",
                code="COMMAND_OWNER_LOST_AFTER_RESTART",
                text=(
                    "The previous command owner ended before a durable terminal "
                    "receipt was confirmed; the command was not replayed."
                ),
                references=(receipt.receipt_fingerprint,),
            )
            try:
                completed = await self._run_storage(
                    self.receipt_storage.complete,
                    absolute_deadline_monotonic=deadline_monotonic,
                    runtime_session_id=self.runtime_session_id,
                    client_instance_id=receipt.client_instance_id,
                    command_id=receipt.command_id,
                    request_semantic_fingerprint=(receipt.request_semantic_fingerprint),
                    outcome=recovery,
                )
            except ValueError:
                winner = await self._run_storage(
                    self.receipt_storage.query,
                    absolute_deadline_monotonic=deadline_monotonic,
                    runtime_session_id=self.runtime_session_id,
                    client_instance_id=receipt.client_instance_id,
                    command_id=receipt.command_id,
                )
                if winner is None or winner.outcome.status == "pending_confirmation":
                    raise RuntimeError(
                        "terminal pending-command recovery has no terminal winner"
                    )
                completed = winner
            record = _CommandRecord(
                semantic_fingerprint=completed.request_semantic_fingerprint,
                outcome=completed.outcome,
            )
            with self._lock:
                self._records[key] = record
                self._order.append(key)
                self._evict_unlocked()

    async def execute(
        self,
        request: TerminalMutationRequest,
        operation: Callable[[], Awaitable[TerminalCommandOutcome]],
    ) -> TerminalCommandOutcome:
        installation = self._start_installation(request, operation)
        record, task = await asyncio.shield(installation)
        return await asyncio.shield(task) if task is not None else record.outcome

    async def start_background(
        self,
        request: TerminalMutationRequest,
        operation: Callable[[], Awaitable[TerminalCommandOutcome]],
    ) -> TerminalCommandOutcome:
        installation = self._start_installation(request, operation)
        record, _task = await asyncio.shield(installation)
        return record.outcome

    def close(self) -> None:
        with self._lock:
            if (
                self._installation_tasks
                or self._physical_storage_operations
                or any(record.task is not None for record in self._records.values())
            ):
                raise RuntimeError(
                    "cannot close terminal command owner with active operations"
                )
            self._closed = True

    def stop_admission(self) -> None:
        with self._lock:
            self._closed = True

    async def drain(self, *, deadline_monotonic: float) -> None:
        """Wait for service-owned operations and receipt retries without cancelling them."""

        self.stop_admission()
        while True:
            with self._lock:
                owner_tasks = tuple(self._installation_tasks) + tuple(
                    record.task
                    for record in self._records.values()
                    if record.task is not None and not record.task.done()
                )
                physical_operations = tuple(self._physical_storage_operations)
            pending_operations = owner_tasks + physical_operations
            if not pending_operations:
                return
            remaining = deadline_monotonic - monotonic()
            if remaining <= 0:
                raise TimeoutError("terminal command owner drain deadline expired")
            done, pending = await asyncio.wait(pending_operations, timeout=remaining)
            for task in done:
                if task in owner_tasks:
                    task.result()
                else:
                    # The logical owner observes and classifies storage failures.
                    # Drain only proves that the physical executor operation exited.
                    task.exception()
            if pending:
                raise TimeoutError("terminal command owners did not drain")

    def _start_installation(
        self,
        request: TerminalMutationRequest,
        operation: Callable[[], Awaitable[TerminalCommandOutcome]],
    ) -> asyncio.Task[
        tuple[_CommandRecord, asyncio.Task[TerminalCommandOutcome] | None]
    ]:
        task = asyncio.create_task(
            self._admit_and_install(request, operation),
            name=(
                f"terminal-command-install:{request.command_kind}:"
                f"{request.binding.command_id}"
            ),
        )
        with self._lock:
            if self._closed:
                task.cancel()
                raise RuntimeError("terminal command admission is closed")
            self._installation_tasks.add(task)
        task.add_done_callback(self._finish_installation)
        return task

    def _finish_installation(
        self,
        task: asyncio.Task[
            tuple[_CommandRecord, asyncio.Task[TerminalCommandOutcome] | None]
        ],
    ) -> None:
        with self._lock:
            self._installation_tasks.discard(task)
        if not task.cancelled():
            # Consume detached-client failures while preserving normal await
            # semantics for callers that still own a subscription.
            task.exception()

    async def _admit_and_install(
        self,
        request: TerminalMutationRequest,
        operation: Callable[[], Awaitable[TerminalCommandOutcome]],
    ) -> tuple[_CommandRecord, asyncio.Task[TerminalCommandOutcome] | None]:
        semantic = terminal_request_semantic_fingerprint(request)
        if (
            semantic != request.request_fingerprint
            or semantic != request.binding.request_semantic_fingerprint
        ):
            raise ValueError("terminal command semantic fingerprint mismatch")
        key = (request.binding.client_instance_id, request.binding.command_id)
        async with self._admission_lock:
            with self._lock:
                if self._closed:
                    raise RuntimeError("terminal command admission is closed")
                existing = self._records.get(key)
                if existing is not None:
                    if existing.semantic_fingerprint != semantic:
                        raise ValueError("terminal command identity conflicts")
                    return existing, existing.task
            pending = _outcome(
                status="pending_confirmation",
                binding=request.binding,
                code="COMMAND_OWNER_RUNNING",
                text="The command is owned and may be queried by its stable ID.",
            )
            admission = await self._run_storage(
                self.receipt_storage.admit_pending,
                runtime_session_id=self.runtime_session_id,
                client_instance_id=request.binding.client_instance_id,
                command_id=request.binding.command_id,
                command_kind=request.command_kind,
                request_semantic_fingerprint=semantic,
                target_id=request.binding.expected_target_id,
                target_generation=request.binding.expected_target_generation,
                pending_outcome=pending,
            )
            durable = admission.receipt
            record = _CommandRecord(
                semantic_fingerprint=semantic,
                outcome=durable.outcome,
            )
            task: asyncio.Task[TerminalCommandOutcome] | None = None
            if admission.execution_owner_won:
                task = asyncio.create_task(
                    self._run_operation(key, operation),
                    name=(
                        f"terminal-command:{request.command_kind}:"
                        f"{request.binding.command_id}"
                    ),
                )
                record.task = task
            with self._lock:
                self._records[key] = record
                self._order.append(key)
                self._evict_unlocked()
            return record, task

    async def _run_operation(
        self,
        key: tuple[str, str],
        operation: Callable[[], Awaitable[TerminalCommandOutcome]],
    ) -> TerminalCommandOutcome:
        try:
            outcome = await operation()
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            with self._lock:
                pending = self._records[key].outcome
            outcome = _replace_outcome(
                pending,
                status="reconciliation_required",
                code="COMMAND_EXECUTION_OWNER_CANCELLED",
                text=(
                    "The command owner was cancelled before the Python authority "
                    "could prove a terminal domain outcome."
                ),
                references=(),
            )
        except BaseException:
            with self._lock:
                pending = self._records[key].outcome
            outcome = _replace_outcome(
                pending,
                status="rejected",
                code="COMMAND_EXECUTION_REJECTED",
                text="The command was rejected by the Python authority.",
                references=(),
            )
        with self._lock:
            record = self._records[key]
            record.terminal_outcome = outcome
        retry_delay = 0.05
        while True:
            try:
                receipt = await self._run_storage(
                    self.receipt_storage.complete,
                    runtime_session_id=self.runtime_session_id,
                    client_instance_id=key[0],
                    command_id=key[1],
                    request_semantic_fingerprint=(
                        self._records[key].semantic_fingerprint
                    ),
                    outcome=outcome,
                )
                outcome = receipt.outcome
                break
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                await _sleep_owned(retry_delay)
                continue
            except ValueError:
                # A conflicting durable terminal winner is not transient.  Re-read
                # it once so an idempotent compatible winner can still be adopted.
                try:
                    winner = await self._run_storage(
                        self.receipt_storage.query,
                        runtime_session_id=self.runtime_session_id,
                        client_instance_id=key[0],
                        command_id=key[1],
                    )
                except BaseException:
                    await _sleep_owned(retry_delay)
                    retry_delay = min(1.0, retry_delay * 2.0)
                    continue
                if (
                    winner is not None
                    and winner.outcome.status != "pending_confirmation"
                ):
                    outcome = winner.outcome
                    break
                # A pending or unreadable winner is not a terminal receipt. Keep
                # the exact candidate owned and fail closed until storage can
                # confirm either it or a compatible terminal winner.
                await _sleep_owned(retry_delay)
                retry_delay = min(1.0, retry_delay * 2.0)
                continue
            except BaseException:
                # The exact outcome remains owned by this task.  Do not expose a
                # process-local terminal answer while durable storage still says
                # pending, and do not lose the result at Host close.
                await _sleep_owned(retry_delay)
                retry_delay = min(1.0, retry_delay * 2.0)
        with self._lock:
            record = self._records[key]
            record.outcome = outcome
            record.terminal_outcome = None
            record.task = None
        return outcome

    async def _run_storage(
        self,
        operation,
        *,
        absolute_deadline_monotonic: float | None = None,
        **values,
    ):
        deadline = (
            monotonic() + self.operation_timeout_seconds
            if absolute_deadline_monotonic is None
            else absolute_deadline_monotonic
        )
        call = partial(operation, deadline_monotonic=deadline, **values)
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("terminal command storage deadline expired")
        future = asyncio.get_running_loop().run_in_executor(self.executor, call)
        with self._lock:
            self._physical_storage_operations.add(future)
        future.add_done_callback(self._finish_physical_storage_operation)
        return await asyncio.wait_for(asyncio.shield(future), timeout=remaining)

    def _finish_physical_storage_operation(
        self, future: asyncio.Future[object]
    ) -> None:
        with self._lock:
            self._physical_storage_operations.discard(future)
        if not future.cancelled():
            # A timed-out or detached waiter may no longer observe this result.
            future.exception()

    def _evict_unlocked(self) -> None:
        while len(self._order) > self.maximum_records:
            candidate = self._order[0]
            record = self._records[candidate]
            if record.task is not None:
                self._order.append(self._order.pop(0))
                if all(self._records[item].task is not None for item in self._order):
                    return
                continue
            self._order.pop(0)
            self._records.pop(candidate, None)


async def _sleep_owned(delay_seconds: float) -> None:
    """Back off without allowing observer cancellation to orphan an owner."""

    while True:
        try:
            await asyncio.sleep(delay_seconds)
            return
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()


class TerminalSessionQueryService:
    def __init__(self, *, host_session) -> None:
        self._host_session = host_session
        self._control_capture = TerminalControlSourceCaptureOwner(
            runtime_session_id=host_session.runtime_session_id
        )
        self._control_projection = TerminalControlProjectionStore(
            runtime_session_id=host_session.runtime_session_id
        )

    def snapshot(self) -> TerminalUiSessionSnapshot:
        return self.snapshot_bundle().session_snapshot

    def read_control_after(
        self, cursor: ControlProjectionCursor
    ) -> ControlProjectionRead:
        # The synchronous snapshot collection runs as one event-loop slice; it
        # refreshes the immutable five-section view before the bounded ring is
        # evaluated.  The store, rather than Gateway, owns the cursor/history.
        self.snapshot_bundle()
        return self._control_projection.read_after(cursor)

    def snapshot_bundle(self) -> TerminalProjectionSnapshotBundle:
        captured = self._control_capture.capture(
            self._read_control_capture_input,
            deadline_monotonic=monotonic() + 10.0,
        )
        control_snapshot = self._control_projection.install_captured(captured)
        return TerminalProjectionSnapshotBundle(
            session_snapshot=captured.capture_input.session_snapshot,
            control_snapshot=control_snapshot,
        )

    def close(self) -> None:
        self._control_capture.close()

    def _read_control_capture_input(self):
        host = self._host_session
        runtime = host.wiring.runtime_wiring.runtime_session
        viewport = runtime.terminal_presentation_foundation_service.snapshot()
        operational = runtime.ui_operational_activity_store.snapshot()
        operational_generation = operational.operational_generation
        operational_cursor = operational.operational_cursor
        queue_checkpoint, queue_snapshot, queue_head_receipt = (
            runtime.prompt_queue_checkpoint_service.client_projection_authority()
        )
        queue_items = tuple(
            _queue_view(item)
            for item in queue_snapshot.items
            if item.delivery_state in CLIENT_VISIBLE_ACTIVE_QUEUE_STATES
            and item.content_retention_state == "active"
        )
        interaction = _interaction_view(host.get_pending_interaction())
        payload = {
            "host_session_id": host.host_session_id,
            "runtime_session_id": host.runtime_session_id,
            "lifecycle": str(host.lifecycle),
            "authority_high_water": viewport.active_head.through_authority_sequence,
            "projection_revision": viewport.projection_revision,
            "viewport_fingerprint": viewport.viewport_fingerprint,
            "operational_generation": operational_generation,
            "operational_cursor": operational_cursor,
            "pending_interaction_fingerprint": (
                interaction.view_fingerprint if interaction is not None else None
            ),
            "queue_head_event_id": queue_snapshot.queue_head_event_id,
            "queue_account_revision": queue_snapshot.account_revision,
            "queue_item_fingerprints": tuple(
                item.view_fingerprint for item in queue_items
            ),
            "active_run_id": host.active_run_id,
            "suspended_run_id": host.suspended_run_id,
            "stopping_run_id": host.stopping_run_id,
        }
        session_snapshot = TerminalUiSessionSnapshot(
            host_session_id=host.host_session_id,
            runtime_session_id=host.runtime_session_id,
            lifecycle=str(host.lifecycle),  # type: ignore[arg-type]
            authority_high_water=viewport.active_head.through_authority_sequence,
            projection_revision=viewport.projection_revision,
            viewport=viewport,
            operational_generation=operational_generation,
            operational_cursor=operational_cursor,
            pending_interaction=interaction,
            queue_items=queue_items,
            queue_head_event_id=queue_snapshot.queue_head_event_id,
            queue_account_revision=queue_snapshot.account_revision,
            active_run_id=host.active_run_id,
            suspended_run_id=host.suspended_run_id,
            stopping_run_id=host.stopping_run_id,
            snapshot_fingerprint=context_fingerprint(
                "terminal-ui-session-snapshot:v1", payload
            ),
        )
        return build_terminal_control_capture_input(
            session_snapshot=session_snapshot,
            queue_checkpoint=queue_checkpoint,
            queue_head_receipt=queue_head_receipt,
            durable_active_item_count=queue_snapshot.active_client_item_count,
            durable_active_item_accumulator=(
                queue_snapshot.active_client_item_accumulator
            ),
        )


def fit_projection_snapshot_bundle_resident_suffix(
    bundle: TerminalProjectionSnapshotBundle,
    *,
    maximum_entries: int,
) -> TerminalProjectionSnapshotBundle:
    """Purely shrink one frozen bundle to its newest resident suffix."""

    session = bundle.session_snapshot
    viewport = fit_viewport_snapshot_resident_suffix(
        session.viewport,
        maximum_entries=maximum_entries,
    )
    if viewport is session.viewport:
        return bundle
    payload = {
        "host_session_id": session.host_session_id,
        "runtime_session_id": session.runtime_session_id,
        "lifecycle": session.lifecycle,
        "authority_high_water": session.authority_high_water,
        "projection_revision": session.projection_revision,
        "viewport_fingerprint": viewport.viewport_fingerprint,
        "operational_generation": session.operational_generation,
        "operational_cursor": session.operational_cursor,
        "pending_interaction_fingerprint": (
            session.pending_interaction.view_fingerprint
            if session.pending_interaction is not None
            else None
        ),
        "queue_head_event_id": session.queue_head_event_id,
        "queue_account_revision": session.queue_account_revision,
        "queue_item_fingerprints": tuple(
            item.view_fingerprint for item in session.queue_items
        ),
        "active_run_id": session.active_run_id,
        "suspended_run_id": session.suspended_run_id,
        "stopping_run_id": session.stopping_run_id,
    }
    fitted = replace(
        session,
        viewport=viewport,
        snapshot_fingerprint=context_fingerprint(
            "terminal-ui-session-snapshot:v1", payload
        ),
    )
    return TerminalProjectionSnapshotBundle(
        session_snapshot=fitted,
        control_snapshot=bundle.control_snapshot,
    )


class _MutationServiceBase:
    def __init__(
        self,
        *,
        host_session,
        attachments: TerminalAttachmentRegistry,
        commands: TerminalCommandOwner,
    ) -> None:
        self.host_session = host_session
        self.attachments = attachments
        self.commands = commands

    def _validate(self, binding: TerminalCommandBinding) -> None:
        self.attachments.validate_controller(binding)
        valid_targets = {
            self.host_session.host_session_id,
            self.host_session.runtime_session_id,
            self.host_session.active_run_id,
            self.host_session.suspended_run_id,
            self.host_session.stopping_run_id,
        }
        if binding.expected_target_generation != 1:
            raise ValueError("terminal command target generation is stale")
        if binding.expected_target_id not in valid_targets:
            raise ValueError("terminal command target identity is stale")


class TerminalPromptSubmissionService(_MutationServiceBase):
    def __init__(
        self,
        *,
        delivery_scheduler: Callable[[str], None],
        **values,
    ) -> None:
        super().__init__(**values)
        self._delivery_scheduler = delivery_scheduler

    async def submit(self, request: SubmitPromptRequest) -> TerminalCommandOutcome:
        async def operation() -> TerminalCommandOutcome:
            self._validate(request.binding)
            host = self.host_session
            active = host.active_run_id is not None or host.stopping_run_id is not None
            if request.requested_delivery_mode == "steer" and not active:
                return _outcome(
                    status="rejected",
                    binding=request.binding,
                    code="STEER_TARGET_UNAVAILABLE",
                    text="Explicit steer requires an active run safe point.",
                )
            if not active:
                try:
                    await host.run_turn(request.text)
                except PresentationHistoryAdmissionRejected as exc:
                    return _outcome(
                        status="rejected",
                        binding=request.binding,
                        code="HISTORY_CAPACITY_REJECTED",
                        text=(
                            "The prompt requires a successor session under the "
                            "history-capacity policy."
                        ),
                        references=(exc.decision.decision_fingerprint,),
                    )
                return _outcome(
                    status="succeeded",
                    binding=request.binding,
                    code="RUN_COMPLETED",
                    text="The submitted prompt completed.",
                )
            runtime = host.wiring.runtime_wiring.runtime_session
            item = await runtime.prompt_queue_mutation_service.submit(
                PromptQueueSubmitRequest(
                    command_id=request.binding.command_id,
                    client_instance_id=request.binding.client_instance_id,
                    client_submission_id=request.client_submission_id,
                    text=request.text,
                    requested_delivery_mode=request.requested_delivery_mode,
                    event_context=_queue_event_context(host),
                )
            )
            outcome = _outcome(
                status="succeeded",
                binding=request.binding,
                code="PROMPT_QUEUED",
                text="The prompt is durably queued.",
                references=(item.head_event_id, item.queue_item_id),
            )
            try:
                self._delivery_scheduler(item.queue_item_id)
            except BaseException:
                # Queue acceptance is already durable authority. Scheduling is
                # only a live acceleration; a close race or wake failure must
                # leave the item for pending-item recovery, never rewrite the
                # command receipt as a rejection that invites duplication.
                pass
            return outcome

        return await self.commands.start_background(request, operation)


class TerminalRunControlService(_MutationServiceBase):
    async def stop(self, request: StopRunRequest) -> TerminalCommandOutcome:
        async def operation() -> TerminalCommandOutcome:
            self._validate(request.binding)
            await self.host_session.stop_current_turn(reason=AbortKind.USER_STOP)
            return _outcome(
                status="succeeded",
                binding=request.binding,
                code="STOP_RESOLVED",
                text="The stop request reached the Host run-control authority.",
            )

        return await self.commands.execute(request, operation)


class TerminalInteractionResolutionService(_MutationServiceBase):
    def __init__(self, *, secret_service: TerminalMcpSecretService, **values) -> None:
        super().__init__(**values)
        self.secret_service = secret_service

    async def resolve(
        self,
        request: ResolveApprovalRequest
        | ResolvePlanQuestionRequest
        | ResolvePlanExitRequest
        | ResolveMcpInteractionRequest
        | CancelMcpInteractionRequest,
    ) -> TerminalCommandOutcome:
        async def operation() -> TerminalCommandOutcome:
            self._validate(request.binding)
            if isinstance(request, ResolveApprovalRequest):
                await self.host_session.resolve_approval(
                    ApprovalResolution(
                        approval_id=request.approval_id,
                        decisions=tuple(
                            ToolApprovalDecision(
                                tool_call_id=tool_call_id, confirmed=confirmed
                            )
                            for tool_call_id, confirmed in request.decisions
                        ),
                    )
                )
            elif isinstance(request, ResolvePlanQuestionRequest):
                await self.host_session.resolve_plan_interaction(
                    PlanQuestionResolution(
                        interaction_id=request.interaction_id,
                        answer_text=request.answer_text,
                        selected_option=request.selected_option,
                    )
                )
            elif isinstance(request, ResolvePlanExitRequest):
                await self.host_session.resolve_plan_interaction(
                    PlanExitResolution(
                        interaction_id=request.interaction_id,
                        decision=request.decision,
                        user_feedback=request.user_feedback,
                    )
                )
            elif isinstance(request, ResolveMcpInteractionRequest):
                responses = self.secret_service.consume_response(
                    handle_id=request.sealed_response_handle_id,
                    binding=request.binding,
                    interaction_id=request.interaction_id,
                )
                await self.host_session.resolve_mcp_input_required(
                    McpInputRequiredInteractionResolution(
                        interaction_id=request.interaction_id,
                        responses=responses,
                    )
                )
                self.secret_service.revoke_interaction(request.interaction_id)
            else:
                await self.host_session.resolve_mcp_input_required(
                    McpInputRequiredInteractionResolution(
                        interaction_id=request.interaction_id,
                        cancelled=True,
                    )
                )
                self.secret_service.revoke_interaction(request.interaction_id)
            return _outcome(
                status="succeeded",
                binding=request.binding,
                code="INTERACTION_RESOLVED",
                text="The interaction resolution was committed by the Host.",
            )

        return await self.commands.start_background(request, operation)


class TerminalPromptQueueMutationServiceAdapter(_MutationServiceBase):
    async def cancel(self, request: QueueCancelRequest) -> TerminalCommandOutcome:
        async def operation() -> TerminalCommandOutcome:
            self._validate(request.binding)
            runtime = self.host_session.wiring.runtime_wiring.runtime_session
            item = await runtime.prompt_queue_mutation_service.cancel(
                queue_item_id=request.queue_item_id,
                command_id=request.binding.command_id,
                event_context=_queue_event_context(self.host_session),
            )
            return _outcome(
                status="succeeded",
                binding=request.binding,
                code="QUEUE_ITEM_CANCELLED",
                text="The queue cancellation is durable.",
                references=(item.head_event_id, item.queue_item_id),
            )

        return await self.commands.execute(request, operation)


class TerminalSessionLifecycleService(_MutationServiceBase):
    def __init__(
        self,
        *,
        detach_callback: Callable[[str], Awaitable[None]] | None = None,
        close_callback: Callable[[str, bool], Awaitable[None]] | None = None,
        successor_callback: (
            Callable[[str, str], Awaitable[tuple[str, str]]] | None
        ) = None,
        **values,
    ) -> None:
        super().__init__(**values)
        self._detach_callback = detach_callback
        self._close_callback = close_callback
        self._successor_callback = successor_callback

    def bind_host_callbacks(
        self,
        *,
        detach_callback: Callable[[str], Awaitable[None]],
        close_callback: Callable[[str, bool], Awaitable[None]],
        successor_callback: Callable[[str, str], Awaitable[tuple[str, str]]],
    ) -> None:
        """Bind the sole HostCore lifecycle authority before publication."""

        if any(
            callback is not None
            for callback in (
                self._detach_callback,
                self._close_callback,
                self._successor_callback,
            )
        ):
            raise RuntimeError("terminal lifecycle callbacks are already bound")
        self._detach_callback = detach_callback
        self._close_callback = close_callback
        self._successor_callback = successor_callback

    async def detach(self, request: DetachSessionRequest) -> TerminalCommandOutcome:
        async def operation() -> TerminalCommandOutcome:
            self.attachments.validate_attachment(request.binding)
            self.attachments.detach(
                attachment_id=request.binding.attachment_id,
                attachment_generation=request.binding.attachment_generation,
            )
            return _outcome(
                status="succeeded",
                binding=request.binding,
                code="ATTACHMENT_DETACHED",
                text=(
                    "The terminal attachment was detached; the runtime remains active."
                ),
            )

        return await self.commands.execute(request, operation)

    async def takeover(
        self, request: ControllerTakeoverRequest
    ) -> TerminalCommandOutcome:
        async def operation() -> TerminalCommandOutcome:
            self.attachments.validate_attachment(request.binding)
            if (
                request.expected_previous_controller_generation
                != self.attachments.controller_generation
            ):
                return _outcome(
                    status="rejected",
                    binding=request.binding,
                    code="CONTROLLER_GENERATION_STALE",
                    text="The controller generation changed before takeover.",
                )
            previous = self.attachments.controller_attachment_id
            lease = self.attachments.takeover(
                attachment_id=request.binding.attachment_id,
                attachment_generation=request.binding.attachment_generation,
            )
            if previous is not None and previous != lease.attachment_id:
                self.host_session.terminal_application_services.secrets.revoke_attachment(
                    previous
                )
            return _outcome(
                status="succeeded",
                binding=request.binding,
                code="CONTROLLER_TAKEOVER_COMMITTED",
                text="Controller ownership moved to this attachment.",
                references=(lease.identity_fingerprint,),
            )

        return await self.commands.execute(request, operation)

    async def close(self, request: CloseSessionRequest) -> TerminalCommandOutcome:
        async def operation() -> TerminalCommandOutcome:
            self._validate(request.binding)
            if self._close_callback is None:
                return _outcome(
                    status="rejected",
                    binding=request.binding,
                    code="SESSION_CLOSE_COORDINATOR_UNAVAILABLE",
                    text="This adapter does not own HostCore session closure.",
                )
            return _outcome(
                status="succeeded",
                binding=request.binding,
                code="SESSION_CLOSE_ACCEPTED",
                text="The Host session close was durably accepted.",
            )

        outcome = await self.commands.execute(request, operation)
        if outcome.status == "succeeded":
            assert self._close_callback is not None
            await self._close_callback(
                self.host_session.host_session_id, request.close_conversation
            )
        return outcome

    async def start_successor(
        self, request: StartSuccessorSessionRequest
    ) -> TerminalCommandOutcome:
        async def operation() -> TerminalCommandOutcome:
            self._validate(request.binding)
            runtime = self.host_session.wiring.runtime_wiring.runtime_session
            capacity = runtime.terminal_presentation_foundation_service.snapshot().active_head.capacity_state
            if (
                request.source_capacity_state_fingerprint
                != capacity.capacity_state_fingerprint
            ):
                return _outcome(
                    status="rejected",
                    binding=request.binding,
                    code="CAPACITY_STATE_STALE",
                    text="The source history-capacity state changed before rotation.",
                    references=(capacity.capacity_state_fingerprint,),
                )
            if self._successor_callback is None:
                return _outcome(
                    status="rejected",
                    binding=request.binding,
                    code="SUCCESSOR_FACTORY_NOT_BOUND",
                    text=(
                        "No successor-session factory is bound to this application "
                        "service."
                    ),
                )
            host_session_id, runtime_session_id = await self._successor_callback(
                self.host_session.runtime_session_id,
                request.source_capacity_state_fingerprint,
            )
            return _outcome(
                status="succeeded",
                binding=request.binding,
                code="SUCCESSOR_SESSION_CREATED",
                text="A new independent Host session was created.",
                references=(host_session_id, runtime_session_id),
            )

        return await self.commands.start_background(request, operation)


@dataclass(slots=True)
class TerminalApplicationServices:
    host_session: object
    detach_callback: Callable[[str], Awaitable[None]] | None = None
    close_callback: Callable[[str, bool], Awaitable[None]] | None = None
    successor_callback: Callable[[str, str], Awaitable[tuple[str, str]]] | None = None
    attachments: TerminalAttachmentRegistry = field(init=False)
    commands: TerminalCommandOwner = field(init=False)
    query: TerminalSessionQueryService = field(init=False)
    prompt_submission: TerminalPromptSubmissionService = field(init=False)
    run_control: TerminalRunControlService = field(init=False)
    interaction: TerminalInteractionResolutionService = field(init=False)
    queue: TerminalPromptQueueMutationServiceAdapter = field(init=False)
    lifecycle: TerminalSessionLifecycleService = field(init=False)
    secrets: TerminalMcpSecretService = field(init=False)
    _queue_delivery_tasks: dict[str, asyncio.Task[object]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _queue_delivery_admission_open: bool = field(
        default=True,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        host = self.host_session
        self.attachments = TerminalAttachmentRegistry(
            runtime_session_id=host.runtime_session_id
        )
        self.commands = TerminalCommandOwner(
            runtime_session_id=host.runtime_session_id,
            receipt_storage=(
                host.wiring.runtime_wiring.runtime_session.terminal_command_receipt_storage
            ),
        )
        self.secrets = TerminalMcpSecretService(
            host_session=host, attachments=self.attachments
        )
        common = {
            "host_session": host,
            "attachments": self.attachments,
            "commands": self.commands,
        }
        self.query = TerminalSessionQueryService(host_session=host)
        self.prompt_submission = TerminalPromptSubmissionService(
            delivery_scheduler=self.schedule_queue_delivery,
            **common,
        )
        self.run_control = TerminalRunControlService(**common)
        self.interaction = TerminalInteractionResolutionService(
            secret_service=self.secrets, **common
        )
        self.queue = TerminalPromptQueueMutationServiceAdapter(**common)
        self.lifecycle = TerminalSessionLifecycleService(
            detach_callback=self.detach_callback,
            close_callback=self.close_callback,
            successor_callback=self.successor_callback,
            **common,
        )

    def bind_host_lifecycle_callbacks(
        self,
        *,
        detach_callback: Callable[[str], Awaitable[None]],
        close_callback: Callable[[str, bool], Awaitable[None]],
        successor_callback: Callable[[str, str], Awaitable[tuple[str, str]]],
    ) -> None:
        self.lifecycle.bind_host_callbacks(
            detach_callback=detach_callback,
            close_callback=close_callback,
            successor_callback=successor_callback,
        )

    def close(self) -> None:
        self._queue_delivery_admission_open = False
        for task in tuple(self._queue_delivery_tasks.values()):
            if not task.done():
                task.cancel()
        self.query.close()
        self.commands.close()
        self.secrets.close()
        self.attachments.close()

    def stop_admission(self) -> None:
        """Reject new mutations and secret leases without detaching observers."""

        self.commands.stop_admission()
        self.secrets.stop_admission()
        self._queue_delivery_admission_open = False

    async def stop_and_drain_commands(
        self,
        *,
        deadline_monotonic: float,
    ) -> None:
        await self.commands.drain(deadline_monotonic=deadline_monotonic)

    async def recover_pending_commands(
        self,
        *,
        deadline_monotonic: float,
    ) -> None:
        await self.commands.recover_pending(deadline_monotonic=deadline_monotonic)

    def schedule_queue_delivery(
        self,
        queue_item_id: str,
    ) -> None:
        existing = self._queue_delivery_tasks.get(queue_item_id)
        if existing is not None and not existing.done():
            return
        if not self._queue_delivery_admission_open:
            raise RuntimeError("terminal queue delivery admission is closed")
        task = asyncio.create_task(
            self._run_queue_delivery_owned(queue_item_id),
            name=f"terminal-queue-delivery:{queue_item_id}",
        )
        self._queue_delivery_tasks[queue_item_id] = task
        task.add_done_callback(
            lambda completed, item_id=queue_item_id: self._finish_queue_delivery_task(
                item_id, completed
            )
        )

    def resume_pending_queue_deliveries(self) -> None:
        """Reinstall process owners for durable queue items after Host open."""

        runtime = self.host_session.wiring.runtime_wiring.runtime_session
        for item in runtime.prompt_queue_projection_store.pending_items(limit=256):
            self.schedule_queue_delivery(item.queue_item_id)

    async def stop_and_drain_queue_deliveries(
        self,
        *,
        deadline_monotonic: float,
    ) -> None:
        self._queue_delivery_admission_open = False
        tasks = tuple(
            task for task in self._queue_delivery_tasks.values() if not task.done()
        )
        if not tasks:
            return
        remaining = deadline_monotonic - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("terminal queue delivery drain deadline expired")
        done, pending = await asyncio.wait(tasks, timeout=remaining)
        for task in done:
            task.exception()
        if pending:
            raise TimeoutError("terminal queue delivery owners did not drain")

    async def retire_terminal_queue_content(
        self,
        *,
        deadline_monotonic: float,
    ) -> None:
        """Release terminal queue content before RuntimeSession teardown."""

        runtime = self.host_session.wiring.runtime_wiring.runtime_session
        reason_by_state = {
            "committed_to_active_run": "terminal_delivery",
            "committed_to_new_run": "terminal_delivery",
            "cancelled": "cancelled",
            "delivery_rejected": "rejected",
        }
        for item in runtime.prompt_queue_projection_store.all_items():
            if item.content_retention_state == "retired":
                continue
            reason = reason_by_state.get(item.delivery_state)
            if reason is None:
                continue
            if asyncio.get_running_loop().time() >= deadline_monotonic:
                raise TimeoutError("queue content retirement deadline expired")
            await runtime.prompt_queue_mutation_service.retire_content(
                queue_item_id=item.queue_item_id,
                command_id=(
                    f"queue-content-retire:{item.queue_item_id}:{item.item_revision}"
                ),
                event_context=(
                    runtime.prompt_queue_mutation_service.source_event_context(
                        item.queue_item_id
                    )
                ),
                reason=reason,
            )

    async def _run_queue_delivery(
        self,
        queue_item_id: str,
    ) -> None:
        runtime = self.host_session.wiring.runtime_wiring.runtime_session
        item = runtime.prompt_queue_projection_store.item(queue_item_id)
        if item is None:
            return
        follow_up_selected = (
            item.delivery_state == "follow_up_reserved"
            or item.delivery_state == "accepted_pending"
            and item.requested_delivery_mode == "follow_up"
        )
        if follow_up_selected:
            if item.delivery_state == "follow_up_reserved":
                reservation = item.reservation
                if reservation is None:
                    raise RuntimeError("reserved follow-up lost its reservation")
                await runtime.prompt_queue_mutation_service.release_reservation(
                    queue_item_id=queue_item_id,
                    reservation_fingerprint=reservation.reservation_fingerprint,
                    command_id=(
                        f"queue-reopen-release:{queue_item_id}:"
                        f"{reservation.reservation_generation}"
                    ),
                    event_context=(
                        runtime.prompt_queue_mutation_service.source_event_context(
                            queue_item_id
                        )
                    ),
                    reason="preflight_retryable",
                )
            try:
                await self.host_session.run_queued_follow_up(queue_item_id)
            except PresentationHistoryAdmissionRejected:
                current = runtime.prompt_queue_projection_store.item(queue_item_id)
                await runtime.prompt_queue_mutation_service.reject_delivery(
                    queue_item_id=queue_item_id,
                    command_id=f"queue-capacity-reject:{queue_item_id}",
                    event_context=(
                        runtime.prompt_queue_mutation_service.source_event_context(
                            queue_item_id
                        )
                    ),
                    reason="history_capacity_rejected",
                    reservation_fingerprint=(
                        current.reservation.reservation_fingerprint
                        if current is not None and current.reservation is not None
                        else None
                    ),
                )
            return
        if item.requested_delivery_mode not in {"auto", "steer"}:
            raise ValueError("queued delivery policy is unknown")
        terminal_states = {
            "committed_to_active_run",
            "committed_to_new_run",
            "cancelled",
            "delivery_rejected",
            "reconciliation_required",
        }
        while True:
            item = runtime.prompt_queue_projection_store.item(queue_item_id)
            if item is None or item.delivery_state in terminal_states:
                return
            reservation = item.reservation
            active_ids = {
                value
                for value in (
                    self.host_session.active_run_id,
                    self.host_session.stopping_run_id,
                )
                if value is not None
            }
            target_run_id = (
                reservation.target_run_id if reservation is not None else None
            )
            target_can_reach_safe_point = (
                item.delivery_state == "accepted_pending"
                and bool(active_ids)
                or item.delivery_state == "steer_reserved"
                and target_run_id in active_ids
            )
            if target_can_reach_safe_point:
                await asyncio.sleep(0.05)
                continue

            event_context = runtime.prompt_queue_mutation_service.source_event_context(
                queue_item_id
            )
            if item.requested_delivery_mode == "auto":
                if reservation is not None:
                    await runtime.prompt_queue_mutation_service.release_reservation(
                        queue_item_id=queue_item_id,
                        reservation_fingerprint=(reservation.reservation_fingerprint),
                        command_id=(
                            f"queue-auto-release:{queue_item_id}:"
                            f"{reservation.reservation_generation}"
                        ),
                        event_context=event_context,
                        reason="safe_point_missed_auto_requeue",
                    )
                try:
                    await self.host_session.run_queued_follow_up(queue_item_id)
                except PresentationHistoryAdmissionRejected:
                    current = runtime.prompt_queue_projection_store.item(queue_item_id)
                    await runtime.prompt_queue_mutation_service.reject_delivery(
                        queue_item_id=queue_item_id,
                        command_id=f"queue-capacity-reject:{queue_item_id}",
                        event_context=event_context,
                        reason="history_capacity_rejected",
                        reservation_fingerprint=(
                            current.reservation.reservation_fingerprint
                            if current is not None and current.reservation is not None
                            else None
                        ),
                    )
                return

            await runtime.prompt_queue_mutation_service.reject_delivery(
                queue_item_id=queue_item_id,
                command_id=(f"queue-steer-reject:{queue_item_id}:{item.item_revision}"),
                event_context=event_context,
                reason="explicit_steer_safe_point_missed",
                reservation_fingerprint=(
                    reservation.reservation_fingerprint
                    if reservation is not None
                    else None
                ),
            )
            return

    async def _run_queue_delivery_owned(self, queue_item_id: str) -> None:
        """Keep one live owner across transient delivery failures."""

        retry_delay = 0.05
        while self._queue_delivery_admission_open:
            try:
                await self._run_queue_delivery(queue_item_id)
                return
            except asyncio.CancelledError:
                raise
            except BaseException:
                runtime = self.host_session.wiring.runtime_wiring.runtime_session
                item = runtime.prompt_queue_projection_store.item(queue_item_id)
                if item is None or item.delivery_state in {
                    "committed_to_active_run",
                    "committed_to_new_run",
                    "cancelled",
                    "delivery_rejected",
                    "reconciliation_required",
                }:
                    return
                await asyncio.sleep(retry_delay)
                retry_delay = min(1.0, retry_delay * 2.0)

    def _finish_queue_delivery_task(
        self,
        queue_item_id: str,
        task: asyncio.Task[object],
    ) -> None:
        if self._queue_delivery_tasks.get(queue_item_id) is task:
            self._queue_delivery_tasks.pop(queue_item_id, None)
        if not task.cancelled():
            task.exception()


def _queue_event_context(host) -> EventContext:
    view = (
        host._run_activation_service.active_host_run_view()
        or host._run_activation_service.suspended_host_run_view()
        or host._run_activation_service.current_host_run_view()
    )
    if view is None:
        raise RuntimeError(
            "prompt queue mutation requires an existing Host run attribution"
        )
    return EventContext(
        run_id=view.run_id,
        turn_id=view.turn_id,
        reply_id=view.reply_id,
    )


def _outcome(
    *,
    status,
    binding: TerminalCommandBinding,
    code: str,
    text: str,
    references: tuple[str, ...] = (),
) -> TerminalCommandOutcome:
    query_token = context_fingerprint(
        "terminal-command-query-token:v1",
        {
            "runtime_session_id": binding.runtime_session_id,
            "client_instance_id": binding.client_instance_id,
            "command_id": binding.command_id,
        },
    )
    public_text = bounded_terminal_safe_public_text(
        text,
        maximum_code_points=512,
        maximum_utf8_bytes=2_048,
    )
    return TerminalCommandOutcome(
        status=status,
        command_id=binding.command_id,
        target_id=binding.expected_target_id,
        target_generation=binding.expected_target_generation,
        public_result_code=code,
        public_result_text=public_text,
        durable_reference_ids=references,
        query_token=query_token,
        outcome_fingerprint=terminal_command_outcome_fingerprint(
            status=status,
            command_id=binding.command_id,
            target_id=binding.expected_target_id,
            target_generation=binding.expected_target_generation,
            public_result_code=code,
            public_result_text=public_text,
            durable_reference_ids=references,
            query_token=query_token,
        ),
    )


def _replace_outcome(
    source: TerminalCommandOutcome,
    *,
    status,
    code: str,
    text: str,
    references: tuple[str, ...] | None = None,
) -> TerminalCommandOutcome:
    durable_references = (
        source.durable_reference_ids if references is None else references
    )
    public_text = bounded_terminal_safe_public_text(
        text,
        maximum_code_points=512,
        maximum_utf8_bytes=2_048,
    )
    return TerminalCommandOutcome(
        status=status,
        command_id=source.command_id,
        target_id=source.target_id,
        target_generation=source.target_generation,
        public_result_code=code,
        public_result_text=public_text,
        durable_reference_ids=durable_references,
        query_token=source.query_token,
        outcome_fingerprint=terminal_command_outcome_fingerprint(
            status=status,
            command_id=source.command_id,
            target_id=source.target_id,
            target_generation=source.target_generation,
            public_result_code=code,
            public_result_text=public_text,
            durable_reference_ids=durable_references,
            query_token=source.query_token,
        ),
    )


def _queue_view(item) -> PromptQueueItemView:
    payload = prompt_queue_item_public_view_payload(item)
    return PromptQueueItemView(
        **payload,
        view_fingerprint=context_fingerprint("prompt-queue-item-view:v1", payload),
    )


def _interaction_view(pending):
    if pending is None:
        return None
    if isinstance(pending, PendingApproval):
        payload = {
            "interaction_kind": "approval",
            "interaction_id": pending.approval_id,
            "run_id": pending.run_id,
            "tool_calls": tuple(
                (
                    item.id,
                    bounded_terminal_safe_public_text(
                        item.name,
                        maximum_code_points=512,
                        maximum_utf8_bytes=2_048,
                    ),
                )
                for item in pending.tool_calls
            ),
        }
        return ApprovalRequestView(
            **payload,
            view_fingerprint=context_fingerprint(
                "terminal-approval-request-view:v1", payload
            ),
        )
    if isinstance(pending, PendingPlanInteraction):
        if pending.kind == "question":
            payload = {
                "interaction_kind": "plan_question",
                "interaction_id": pending.interaction_id,
                "run_id": pending.run_id,
                "question": bounded_terminal_safe_public_text(
                    pending.question,
                    maximum_code_points=8_000,
                    maximum_utf8_bytes=32_000,
                ),
                "options": tuple(
                    (
                        bounded_terminal_safe_public_text(
                            item.label,
                            maximum_code_points=512,
                            maximum_utf8_bytes=2_048,
                        ),
                        bounded_terminal_safe_public_text(
                            item.description,
                            maximum_code_points=2_000,
                            maximum_utf8_bytes=8_000,
                        ),
                    )
                    for item in pending.options
                ),
                "allow_free_text": pending.allow_free_text,
            }
            return PlanQuestionView(
                **payload,
                view_fingerprint=context_fingerprint(
                    "terminal-plan-question-view:v1", payload
                ),
            )
        payload = {
            "interaction_kind": "plan_exit",
            "interaction_id": pending.interaction_id,
            "run_id": pending.run_id,
            "summary": bounded_terminal_safe_public_text(
                pending.summary,
                maximum_code_points=8_000,
                maximum_utf8_bytes=32_000,
            ),
            "plan_artifact_id": pending.plan_artifact_id,
        }
        return PlanExitView(
            **payload,
            view_fingerprint=context_fingerprint("terminal-plan-exit-view:v1", payload),
        )
    if not isinstance(pending, PendingMcpInputRequired):
        raise TypeError("terminal pending interaction kind is unknown")
    requests = []
    for item in pending.input_requests:
        mode = str(item.get("mode", "form"))
        if mode not in {"form", "url"}:
            raise ValueError("MCP public interaction mode is unknown")
        requests.append(
            McpInputRequestPublicView(
                request_key=str(item.get("key", "")),
                mode=mode,  # type: ignore[arg-type]
                public_prompt=bounded_terminal_safe_public_text(
                    str(item.get("message") or item.get("title") or ""),
                    maximum_code_points=8_000,
                    maximum_utf8_bytes=32_000,
                ),
                schema_or_url_present=(
                    bool(item.get("requestedSchema")) if mode == "form" else True
                ),
            )
        )
    payload = {
        "interaction_kind": "mcp_input_required",
        "interaction_id": pending.interaction_id,
        "run_id": pending.run_id,
        "tool_call_id": pending.tool_call_id,
        "tool_name": bounded_terminal_safe_public_text(
            pending.tool_name,
            maximum_code_points=512,
            maximum_utf8_bytes=2_048,
        ),
        "server_id": pending.server_id,
        "requests": tuple(asdict(item) for item in requests),
    }
    return McpInteractionView(
        interaction_kind="mcp_input_required",
        interaction_id=pending.interaction_id,
        run_id=pending.run_id,
        tool_call_id=pending.tool_call_id,
        tool_name=payload["tool_name"],
        server_id=pending.server_id,
        requests=tuple(requests),
        view_fingerprint=context_fingerprint(
            "terminal-mcp-interaction-view:v1", payload
        ),
    )


__all__ = [
    "TerminalApplicationServices",
    "TerminalCommandOwner",
    "TerminalInteractionResolutionService",
    "TerminalPromptQueueMutationServiceAdapter",
    "TerminalPromptSubmissionService",
    "TerminalRunControlService",
    "TerminalSessionLifecycleService",
    "TerminalSessionQueryService",
    "terminal_request_semantic_fingerprint",
]
