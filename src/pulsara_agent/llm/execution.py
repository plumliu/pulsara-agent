"""Session-owned model-stream execution handles.

The execution task, rather than any subscriber, owns transport progress and
durable event production.  Subscriptions are bounded live-observation views of
already committed events and may detach without cancelling the worker.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, cast
from uuid import uuid4

from pulsara_agent.event import AgentEvent, ModelCallEndEvent, ModelCallStartEvent
from pulsara_agent.llm.coalescing import (
    ArbiterSignalStamp,
    ModelStreamInputArbiter,
)
from pulsara_agent.primitives.model_call import sha256_fingerprint

if TYPE_CHECKING:
    from pulsara_agent.llm.coalescing import ModelStreamCoalescingCoordinator
    from pulsara_agent.primitives.model_call import CommittedModelCallResult


class ModelStreamSubscriptionCloseReason(StrEnum):
    TERMINAL_OBSERVED = "terminal_observed"
    DETACHED_BY_CALLER = "detached_by_caller"
    OBSERVER_LAGGED = "observer_lagged"
    OWNER_CANCELLED = "owner_cancelled"
    RECONCILIATION_BLOCKED = "reconciliation_blocked"


class ModelStreamExecutionDrainBlocked(RuntimeError):
    """At least one stream has no trustworthy terminal/physical outcome."""


@dataclass(frozen=True, slots=True)
class ModelStreamSubscriptionClosed:
    close_reason: ModelStreamSubscriptionCloseReason
    last_confirmed_sequence: int | None
    terminal_sequence: int | None
    can_resume_from_cursor: bool


@dataclass(frozen=True, slots=True)
class ModelStreamCompletion:
    resolved_model_call_id: str
    terminal_outcome: Literal[
        "completed",
        "provider_error",
        "cancelled",
        "runtime_error",
        "rejected_before_start",
        "reconciliation_blocked",
    ]
    committed_events: tuple[AgentEvent, ...]
    diagnostic_code: str | None


_SUBSCRIPTION_END = object()


@dataclass(slots=True)
class ModelStreamLiveSemanticCursor:
    """Process-local proof for one live stream's committed semantic prefix."""

    resolved_model_call_id: str
    model_call_start_event_id: str
    start_sequence: int
    confirmed_source_item_count: int = 0
    confirmed_source_accumulator: str = field(
        default_factory=lambda: sha256_fingerprint(
            "model-stream-sanitized-source:v2", "empty"
        )
    )
    confirmed_durable_event_count: int = 0
    last_semantic_event_id: str | None = None
    terminal_event_id: str | None = None

    def require_open(
        self,
        *,
        resolved_model_call_id: str,
        model_call_start_event_id: str,
        expected_source_item_count: int,
        expected_source_accumulator: str,
        expected_durable_event_count: int,
        expected_previous_semantic_event_id: str | None,
    ) -> None:
        if (
            self.resolved_model_call_id != resolved_model_call_id
            or self.model_call_start_event_id != model_call_start_event_id
        ):
            raise RuntimeError("model stream live semantic cursor identity drifted")
        if self.terminal_event_id is not None:
            raise RuntimeError("model stream live semantic cursor is terminal")
        if (
            self.confirmed_source_item_count != expected_source_item_count
            or self.confirmed_source_accumulator != expected_source_accumulator
            or self.confirmed_durable_event_count != expected_durable_event_count
            or self.last_semantic_event_id != expected_previous_semantic_event_id
        ):
            raise RuntimeError("model stream live semantic cursor drifted")

    def advance_semantic(self, events: tuple[AgentEvent, ...]) -> None:
        if not events:
            raise ValueError("semantic cursor advance requires committed events")
        if self.terminal_event_id is not None:
            raise RuntimeError("terminal model stream cannot advance semantics")
        for event in events:
            attribution = getattr(event, "model_stream_attribution", None)
            if attribution is None:
                raise ValueError("semantic cursor advance requires attribution")
            span = attribution.source_span
            if (
                attribution.durable_semantic_event_index
                != self.confirmed_durable_event_count
                or span.first_transport_sequence_index
                != self.confirmed_source_item_count
                or span.source_accumulator_before
                != self.confirmed_source_accumulator
            ):
                raise RuntimeError("model stream semantic cursor continuity drifted")
            self.confirmed_source_item_count += span.source_item_count
            self.confirmed_source_accumulator = span.source_accumulator_after
            self.confirmed_durable_event_count += 1
            self.last_semantic_event_id = event.id

    def mark_terminal(self, event: ModelCallEndEvent) -> None:
        if self.terminal_event_id is not None and self.terminal_event_id != event.id:
            raise RuntimeError("model stream terminal identity drifted")
        self.terminal_event_id = event.id


class ModelStreamNotificationSubscription:
    def __init__(
        self,
        *,
        handle: "ModelStreamExecutionHandle",
        subscription_id: int,
        queue: asyncio.Queue[AgentEvent | object],
        notification_capacity: int,
    ) -> None:
        self._handle = handle
        self._subscription_id = subscription_id
        self._queue = queue
        self._notification_capacity = notification_capacity
        self._closed: asyncio.Future[ModelStreamSubscriptionClosed] = (
            asyncio.get_running_loop().create_future()
        )
        self._last_confirmed_sequence: int | None = None
        self._pending_close: ModelStreamSubscriptionClosed | None = None

    async def __aenter__(self) -> "ModelStreamNotificationSubscription":
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.detach()

    def __aiter__(self) -> "ModelStreamNotificationSubscription":
        return self

    async def __anext__(self) -> AgentEvent:
        item = await self._queue.get()
        if item is _SUBSCRIPTION_END:
            self._complete_pending_close()
            raise StopAsyncIteration
        event = cast(AgentEvent, item)
        self._last_confirmed_sequence = event.sequence
        return event

    async def detach(self) -> ModelStreamSubscriptionClosed:
        self._handle._detach_subscription(
            self._subscription_id,
            reason=ModelStreamSubscriptionCloseReason.DETACHED_BY_CALLER,
        )
        return await asyncio.shield(self._closed)

    async def aclose(self) -> ModelStreamSubscriptionClosed:
        return await self.detach()

    async def wait_closed(self) -> ModelStreamSubscriptionClosed:
        return await asyncio.shield(self._closed)

    def _finish(
        self,
        closed: ModelStreamSubscriptionClosed,
        *,
        discard_pending: bool = False,
    ) -> None:
        self._pending_close = closed
        if discard_pending:
            while True:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            self._complete_pending_close()
        self._queue.put_nowait(_SUBSCRIPTION_END)

    def _complete_pending_close(self) -> None:
        if self._closed.done():
            return
        pending = self._pending_close
        if pending is None:
            raise RuntimeError("model stream subscription ended without close state")
        self._closed.set_result(
            ModelStreamSubscriptionClosed(
                close_reason=pending.close_reason,
                last_confirmed_sequence=self._last_confirmed_sequence,
                terminal_sequence=pending.terminal_sequence,
                can_resume_from_cursor=pending.can_resume_from_cursor,
            )
        )
        self._handle._forget_subscription(
            self._subscription_id,
            subscription=self,
        )

    def _enqueue_committed(self, event: AgentEvent) -> None:
        if self._queue.qsize() >= self._notification_capacity:
            raise asyncio.QueueFull
        self._queue.put_nowait(event)

    @property
    def last_confirmed_sequence(self) -> int | None:
        return self._last_confirmed_sequence


class ModelStreamExecutionHandle:
    def __init__(
        self,
        *,
        handle_id: str,
        handle_generation: int,
        run_id: str,
        resolved_model_call_id: str,
        mailbox_size: int,
        subscription_start_sequence: int,
    ) -> None:
        self.handle_id = handle_id
        self.handle_generation = handle_generation
        self.run_id = run_id
        self.resolved_model_call_id = resolved_model_call_id
        self._mailbox_size = mailbox_size
        self._subscription_start_sequence = subscription_start_sequence
        self._committed_history: list[AgentEvent] = []
        self._committed_history_truncated = False
        loop = asyncio.get_running_loop()
        self.completion: asyncio.Future[ModelStreamCompletion] = loop.create_future()
        self.result: asyncio.Future[CommittedModelCallResult] = loop.create_future()
        self.result.add_done_callback(_observe_future_exception)
        self._task: asyncio.Task[None] | None = None
        self._activation_gate = asyncio.Event()
        self._cancel_requested = asyncio.Event()
        self._input_arbiter = ModelStreamInputArbiter()
        self._coalescing_owner: ModelStreamCoalescingCoordinator | None = None
        self._subscriptions: dict[int, ModelStreamNotificationSubscription] = {}
        self._next_subscription_id = 1
        self._cancel_reason: Literal["user_stop", "host_teardown"] | None = None
        self._cancel_stamp: ArbiterSignalStamp | None = None
        self._physical_operations: dict[str, asyncio.Future[object]] = {}
        self._live_semantic_cursor: ModelStreamLiveSemanticCursor | None = None

    def install_live_semantic_cursor(
        self, start: ModelCallStartEvent
    ) -> ModelStreamLiveSemanticCursor:
        if start.sequence is None:
            raise ValueError("model stream start must be committed before cursor install")
        if start.resolved_call.resolved_model_call_id != self.resolved_model_call_id:
            raise ValueError("model stream start/cursor call identity mismatch")
        candidate = ModelStreamLiveSemanticCursor(
            resolved_model_call_id=self.resolved_model_call_id,
            model_call_start_event_id=start.id,
            start_sequence=start.sequence,
        )
        if self._live_semantic_cursor is not None:
            if self._live_semantic_cursor != candidate:
                raise RuntimeError("model stream live semantic cursor already differs")
            return self._live_semantic_cursor
        self._live_semantic_cursor = candidate
        return candidate

    def require_live_semantic_cursor(self) -> ModelStreamLiveSemanticCursor:
        cursor = self._live_semantic_cursor
        if cursor is None:
            raise RuntimeError("model stream live semantic cursor is not installed")
        return cursor

    def subscribe(
        self, *, after_sequence: int | None = None
    ) -> ModelStreamNotificationSubscription:
        if after_sequence is not None and after_sequence < 0:
            raise ValueError("model stream subscription cursor cannot be negative")
        subscription_id = self._next_subscription_id
        self._next_subscription_id += 1
        subscription = ModelStreamNotificationSubscription(
            handle=self,
            subscription_id=subscription_id,
            queue=asyncio.Queue(maxsize=self._mailbox_size + 1),
            notification_capacity=self._mailbox_size,
        )
        cursor = (
            self._subscription_start_sequence
            if after_sequence is None
            else after_sequence
        )
        first_retained_sequence = next(
            (
                event.sequence
                for event in self._committed_history
                if event.sequence is not None
            ),
            None,
        )
        history_gap = (
            self._committed_history_truncated
            and first_retained_sequence is not None
            and cursor < first_retained_sequence - 1
        )
        catch_up = tuple(
            event
            for event in self._committed_history
            if event.sequence is not None and event.sequence > cursor
        )
        if history_gap or len(catch_up) > self._mailbox_size:
            subscription._finish(
                ModelStreamSubscriptionClosed(
                    close_reason=ModelStreamSubscriptionCloseReason.OBSERVER_LAGGED,
                    last_confirmed_sequence=cursor,
                    terminal_sequence=None,
                    can_resume_from_cursor=True,
                ),
                discard_pending=True,
            )
            return subscription
        for event in catch_up:
            subscription._enqueue_committed(event)
        if self.completion.done():
            completion = self.completion.result()
            subscription._finish(
                _closed_from_completion(
                    completion,
                    last_confirmed_sequence=(
                        subscription.last_confirmed_sequence
                    ),
                )
            )
        else:
            self._subscriptions[subscription_id] = subscription
        return subscription

    async def request_cancel(
        self, *, reason: Literal["user_stop", "host_teardown"]
    ) -> None:
        if self._cancel_reason is None:
            self._cancel_stamp = self._input_arbiter.stamp()
            self._cancel_reason = reason
            self._cancel_requested.set()

    @property
    def input_arbiter(self) -> ModelStreamInputArbiter:
        return self._input_arbiter

    def install_coalescing_owner(
        self,
        owner: ModelStreamCoalescingCoordinator,
    ) -> None:
        if self._coalescing_owner is not None:
            raise RuntimeError("model stream coalescing owner is already installed")
        self._coalescing_owner = owner

    @property
    def coalescing_owned_candidate_count(self) -> int:
        owner = self._coalescing_owner
        return owner.owned_candidate_count if owner is not None else 0

    async def wait_until_activated(self) -> None:
        await self._activation_gate.wait()

    def activate(self) -> None:
        self._activation_gate.set()

    def cancellation_reason(
        self,
    ) -> Literal["user_stop", "host_teardown"] | None:
        return self._cancel_reason

    async def wait_cancellation_requested(
        self,
    ) -> tuple[Literal["user_stop", "host_teardown"], ArbiterSignalStamp]:
        await self._cancel_requested.wait()
        reason = self._cancel_reason
        stamp = self._cancel_stamp
        if (
            reason is None or stamp is None
        ):  # defensive against an invalid manual Event mutation
            raise RuntimeError("model stream cancellation signal lost its reason")
        return reason, stamp

    def register_physical_operation(
        self, operation: asyncio.Future[object]
    ) -> str:
        operation_id = f"model_physical:{uuid4().hex}"
        self._physical_operations[operation_id] = operation
        return operation_id

    def complete_physical_operation(self, operation_id: str) -> None:
        self._physical_operations.pop(operation_id, None)

    def has_physical_operations(self) -> bool:
        return bool(self._physical_operations)

    async def wait_completed(self) -> ModelStreamCompletion:
        return await asyncio.shield(self.completion)

    async def wait_result(self) -> "CommittedModelCallResult":
        return await asyncio.shield(self.result)

    def _set_result(self, result: "CommittedModelCallResult") -> None:
        if self.result.done():
            if self.result.result() != result:
                raise RuntimeError("model stream result was already set differently")
            return
        self.result.set_result(result)

    def _set_result_error(self, error: BaseException) -> None:
        if self.result.done():
            return
        self.result.set_exception(error)

    def _bind_task(self, task: asyncio.Task[None]) -> None:
        if self._task is not None:
            raise RuntimeError("model stream handle task is already installed")
        self._task = task

    def _publish_committed(self, event: AgentEvent) -> None:
        self._committed_history.append(event)
        if len(self._committed_history) > self._mailbox_size:
            self._committed_history.pop(0)
            self._committed_history_truncated = True
        lagged: list[int] = []
        for subscription_id, subscription in self._subscriptions.items():
            try:
                subscription._enqueue_committed(event)
            except asyncio.QueueFull:
                lagged.append(subscription_id)
        for subscription_id in lagged:
            self._detach_subscription(
                subscription_id,
                reason=ModelStreamSubscriptionCloseReason.OBSERVER_LAGGED,
            )

    def _finish(self, completion: ModelStreamCompletion) -> None:
        if not self.completion.done():
            self.completion.set_result(completion)
        for subscription in tuple(self._subscriptions.values()):
            subscription._finish(
                _closed_from_completion(
                    completion,
                    last_confirmed_sequence=(
                        subscription.last_confirmed_sequence
                    ),
                )
            )

    def _fail(self, error: BaseException) -> None:
        if not self.completion.done():
            self.completion.set_exception(error)
        if not self.result.done():
            self.result.set_exception(error)
        closed = ModelStreamSubscriptionClosed(
            close_reason=ModelStreamSubscriptionCloseReason.RECONCILIATION_BLOCKED,
            last_confirmed_sequence=None,
            terminal_sequence=None,
            can_resume_from_cursor=False,
        )
        for subscription in tuple(self._subscriptions.values()):
            subscription._finish(closed, discard_pending=True)

    def _forget_subscription(
        self,
        subscription_id: int,
        *,
        subscription: ModelStreamNotificationSubscription,
    ) -> None:
        if self._subscriptions.get(subscription_id) is subscription:
            self._subscriptions.pop(subscription_id, None)

    def _detach_subscription(
        self,
        subscription_id: int,
        *,
        reason: ModelStreamSubscriptionCloseReason,
    ) -> None:
        subscription = self._subscriptions.pop(subscription_id, None)
        if subscription is None:
            return
        subscription._finish(
            ModelStreamSubscriptionClosed(
                close_reason=reason,
                last_confirmed_sequence=(
                    subscription.last_confirmed_sequence
                ),
                terminal_sequence=None,
                can_resume_from_cursor=True,
            ),
            discard_pending=True,
        )


class ModelStreamExecutionRegistry:
    def __init__(self, *, mailbox_size: int = 256) -> None:
        if mailbox_size < 1:
            raise ValueError("model stream mailbox size must be positive")
        self._mailbox_size = mailbox_size
        self._generation = 0
        self._handles: dict[str, ModelStreamExecutionHandle] = {}

    def install_and_start(
        self,
        *,
        handle_id: str,
        run_id: str,
        resolved_model_call_id: str,
        subscription_start_sequence: int,
        worker: Callable[
            [ModelStreamExecutionHandle], Awaitable[ModelStreamCompletion]
        ],
    ) -> ModelStreamExecutionHandle:
        if handle_id in self._handles:
            raise ValueError(f"model stream handle already exists: {handle_id}")
        self._generation += 1
        handle = ModelStreamExecutionHandle(
            handle_id=handle_id,
            handle_generation=self._generation,
            run_id=run_id,
            resolved_model_call_id=resolved_model_call_id,
            mailbox_size=self._mailbox_size,
            subscription_start_sequence=subscription_start_sequence,
        )
        self._handles[handle_id] = handle

        async def run_owned() -> None:
            retain_owner = False
            try:
                completion = await worker(handle)
            except BaseException as exc:
                handle._fail(exc)
            else:
                handle._finish(completion)
                retain_owner = (
                    completion.terminal_outcome == "reconciliation_blocked"
                )
            finally:
                if not retain_owner and self._handles.get(handle_id) is handle:
                    self._handles.pop(handle_id, None)

        owned_coroutine = run_owned()
        try:
            task = asyncio.create_task(
                owned_coroutine,
                name=f"model-stream:{handle_id}",
            )
        except BaseException:
            owned_coroutine.close()
            if self._handles.get(handle_id) is handle:
                self._handles.pop(handle_id, None)
            raise
        handle._bind_task(task)
        handle.activate()
        return handle

    async def drain_all(self, *, deadline_monotonic: float) -> None:
        handles = tuple(self._handles.values())
        for handle in handles:
            await handle.request_cancel(reason="host_teardown")
        timeout = max(0.0, deadline_monotonic - asyncio.get_running_loop().time())
        if not handles:
            return
        try:
            async with asyncio.timeout(timeout):
                completions = await asyncio.gather(
                    *(handle.wait_completed() for handle in handles),
                    return_exceptions=False,
                )
        except TimeoutError as exc:
            raise TimeoutError("model stream execution drain exceeded deadline") from exc
        if any(
            completion.terminal_outcome == "reconciliation_blocked"
            for completion in completions
        ) or any(handle.has_physical_operations() for handle in handles):
            raise ModelStreamExecutionDrainBlocked(
                "model stream reconciliation/physical state blocks teardown"
            )

    async def request_cancel_run(
        self,
        run_id: str,
        *,
        reason: Literal["user_stop", "host_teardown"],
    ) -> int:
        handles = tuple(
            handle for handle in self._handles.values() if handle.run_id == run_id
        )
        for handle in handles:
            await handle.request_cancel(reason=reason)
        return len(handles)

    async def drain_run(
        self,
        run_id: str,
        *,
        reason: Literal["user_stop", "host_teardown"],
        deadline_monotonic: float,
    ) -> None:
        handles = tuple(
            handle for handle in self._handles.values() if handle.run_id == run_id
        )
        for handle in handles:
            await handle.request_cancel(reason=reason)
        if not handles:
            return
        timeout = max(0.0, deadline_monotonic - asyncio.get_running_loop().time())
        try:
            async with asyncio.timeout(timeout):
                completions = await asyncio.gather(
                    *(handle.wait_completed() for handle in handles),
                    return_exceptions=False,
                )
        except TimeoutError as exc:
            raise TimeoutError(
                f"model stream execution for run {run_id} exceeded deadline"
            ) from exc
        if any(
            completion.terminal_outcome == "reconciliation_blocked"
            for completion in completions
        ) or any(handle.has_physical_operations() for handle in handles):
            raise ModelStreamExecutionDrainBlocked(
                f"model stream execution for run {run_id} blocks teardown"
            )

    def active_handle_count_for_run(self, run_id: str) -> int:
        return sum(handle.run_id == run_id for handle in self._handles.values())

    def active_handle_count(self) -> int:
        return len(self._handles)


def _closed_from_completion(
    completion: ModelStreamCompletion,
    *,
    last_confirmed_sequence: int | None = None,
) -> ModelStreamSubscriptionClosed:
    terminal_sequence = next(
        (
            event.sequence
            for event in reversed(completion.committed_events)
            if isinstance(event, ModelCallEndEvent)
        ),
        None,
    )
    reason = (
        ModelStreamSubscriptionCloseReason.RECONCILIATION_BLOCKED
        if completion.terminal_outcome == "reconciliation_blocked"
        else ModelStreamSubscriptionCloseReason.OWNER_CANCELLED
        if completion.terminal_outcome == "cancelled"
        else ModelStreamSubscriptionCloseReason.TERMINAL_OBSERVED
    )
    return ModelStreamSubscriptionClosed(
        close_reason=reason,
        last_confirmed_sequence=last_confirmed_sequence,
        terminal_sequence=terminal_sequence,
        can_resume_from_cursor=False,
    )


def _observe_future_exception(future: asyncio.Future[object]) -> None:
    if future.cancelled():
        return
    try:
        future.exception()
    except BaseException:
        pass


__all__ = [
    "ModelStreamCompletion",
    "ModelStreamExecutionHandle",
    "ModelStreamExecutionDrainBlocked",
    "ModelStreamExecutionRegistry",
    "ModelStreamLiveSemanticCursor",
    "ModelStreamNotificationSubscription",
    "ModelStreamSubscriptionClosed",
    "ModelStreamSubscriptionCloseReason",
]
