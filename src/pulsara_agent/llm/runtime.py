"""Resolved-target LLM runtime."""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import TYPE_CHECKING
from uuid import uuid4

from pulsara_agent.event import (
    AgentEvent,
    DataBlockDeltaEvent,
    DataBlockEndEvent,
    DataBlockStartEvent,
    EventContext,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ProviderModelStreamErrorEvent,
    ReplyEndEvent,
    RolloutBudgetReservationSettledEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ThinkingBlockDeltaEvent,
    ThinkingBlockEndEvent,
    ThinkingBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from pulsara_agent.llm.drafts import (
    ProviderDataBlockDeltaDraft,
    ProviderDataBlockEndDraft,
    ProviderDataBlockStartDraft,
    ProviderErrorDraft,
    ProviderTextBlockDeltaDraft,
    ProviderTextBlockEndDraft,
    ProviderTextBlockStartDraft,
    ProviderThinkingBlockDeltaDraft,
    ProviderThinkingBlockEndDraft,
    ProviderThinkingBlockStartDraft,
    ProviderToolCallDeltaDraft,
    ProviderToolCallEndDraft,
    ProviderToolCallStartDraft,
    ProviderTransportSemanticDraft,
    ProviderTransportTerminalDraft,
)
from pulsara_agent.llm.commit import (
    ConfirmedCommittedBatch,
    ModelStreamCommitNotCommitted,
    ModelStreamSemanticCommitGuard,
    RuntimeSessionModelStreamEventCommitPort,
    build_model_stream_start_commit_guard,
    build_model_stream_terminal_commit_guard,
)
from pulsara_agent.event_log.serialization import freeze_event_write_candidate
from pulsara_agent.llm.config import LLMConfig
from pulsara_agent.llm.models import ModelRole
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.llm.resolution import (
    ResolvedModelCall,
    ResolvedModelTarget,
    rebind_model_target,
    resolve_model_call,
    resolve_model_target,
)
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.llm.validation import validate_model_context_for_call
from pulsara_agent.primitives.model_call import (
    ModelCallPurpose,
    ModelStreamSemanticAttributionFact,
    ResolvedModelTargetFact,
    sha256_fingerprint,
)
from pulsara_agent.primitives.run_boundary import (
    ModelStreamRecoveryPlanFact,
)
from pulsara_agent.primitives.long_horizon import (
    RolloutReservationFact,
)
from pulsara_agent.llm.errors import LLMTransportContractError
from pulsara_agent.llm.execution import (
    ModelStreamCompletion,
    ModelStreamExecutionHandle,
    ModelStreamExecutionRegistry,
)
from pulsara_agent.llm.lifecycle import (
    ModelLifecycleStartCommitBundle,
    validate_model_lifecycle_start_bundle,
)
from pulsara_agent.llm.materialize import (
    ModelStreamMaterializationError,
    materialize_committed_model_call_result,
)
from pulsara_agent.llm.sanitizing_transport import (
    ProviderTransportPhysicalCompletionStatus,
)
from pulsara_agent.llm.accounting import (
    build_model_reservation_settlement_event,
)

if TYPE_CHECKING:
    from pulsara_agent.runtime.session import RuntimeSession


_SEMANTIC_BATCH_MAX_EVENTS = 16
_SEMANTIC_BATCH_MAX_CHARS = 4_096
_SEMANTIC_BATCH_MAX_AGE_SECONDS = 0.025


def _semantic_event_content_chars(event: AgentEvent) -> int:
    if isinstance(
        event,
        (TextBlockDeltaEvent, ThinkingBlockDeltaEvent, ToolCallDeltaEvent),
    ):
        return len(event.delta)
    if isinstance(event, DataBlockDeltaEvent):
        return len(event.data)
    return 0


class LLMRuntime:
    def __init__(self, *, config: LLMConfig, registry: LLMTransportRegistry) -> None:
        self._config = config
        self._registry = registry

    def resolve_target(
        self,
        *,
        role: ModelRole,
        requested_options: LLMOptions | None = None,
    ) -> ResolvedModelTarget:
        return resolve_model_target(
            config=self._config,
            registry=self._registry,
            role=role,
            requested_options=requested_options,
        )

    def resolve_call(
        self,
        *,
        target: ResolvedModelTarget,
        purpose: ModelCallPurpose,
    ) -> ResolvedModelCall:
        return resolve_model_call(target=target, purpose=purpose)

    def rebind_target(self, fact: ResolvedModelTargetFact) -> ResolvedModelTarget:
        return rebind_model_target(
            config=self._config, registry=self._registry, fact=fact
        )

    def start_stream(
        self,
        *,
        call: ResolvedModelCall,
        context: LLMContext,
        event_context: EventContext,
        start_bundle: ModelLifecycleStartCommitBundle,
        commit_port: RuntimeSessionModelStreamEventCommitPort,
        execution_registry: ModelStreamExecutionRegistry,
    ) -> ModelStreamExecutionHandle:
        """Start one session-owned stream worker.

        The process owner is installed before validation, reservation, recovery
        planning, or transport construction can run.  Once activated, the worker
        owns those steps and every durable lifecycle/semantic write; subscribers
        only observe committed events.
        """

        runtime_session = commit_port.runtime_session
        call_id = call.fact.resolved_model_call_id
        handle_id = f"model_stream:{call_id}:{uuid4().hex}"

        async def worker(handle: ModelStreamExecutionHandle) -> ModelStreamCompletion:
            await handle.wait_until_activated()
            committed: list[AgentEvent] = []
            start_committed = False
            terminal_committed = False
            semantic_item_count = 0
            last_semantic_event_id: str | None = None
            live_semantic_cursor = None

            try:
                validate_model_lifecycle_start_bundle(
                    start_bundle,
                    call=call,
                    context=context,
                    event_context=event_context,
                )
                validation = validate_model_context_for_call(
                    call=call,
                    context=context,
                )
                recovery_plan = start_bundle.recovery_plan
                rollout_reservation = start_bundle.reservation
                if (
                    validation.estimate.total_input_tokens
                    != recovery_plan.pre_send_estimated_input_tokens
                ):
                    raise ValueError(
                        "model lifecycle estimate changed during final validation"
                    )
            except Exception as exc:
                handle._set_result_error(exc)
                return ModelStreamCompletion(
                    resolved_model_call_id=call_id,
                    terminal_outcome="rejected_before_start",
                    committed_events=(),
                    diagnostic_code="model_stream_rejected_before_start",
                )

            def record_commit(
                result: ConfirmedCommittedBatch,
            ) -> tuple[AgentEvent, ...]:
                nonlocal start_committed, terminal_committed
                stored = result.decode_owned()
                committed.extend(stored)
                for event in stored:
                    handle._publish_committed(event)
                start_committed = start_committed or any(
                    isinstance(event, ModelCallStartEvent) for event in stored
                )
                terminal_committed = terminal_committed or any(
                    isinstance(event, ModelCallEndEvent) for event in stored
                )
                return stored

            async def materialize_terminal_result() -> bool:
                try:
                    deadline = monotonic() + 30.0
                    result = await runtime_session.context_input_io_service.execute(
                        operation_name="model-call-result-materialize",
                        operation=lambda: materialize_committed_model_call_result(
                            runtime_session.event_log,
                            resolved_model_call_id=call_id,
                            deadline_monotonic=deadline,
                        ),
                        deadline_monotonic=deadline,
                    )
                except BaseException:
                    runtime_session.latch_event_commit_outcome_unknown()
                    handle._set_result_error(
                        ModelStreamMaterializationError(
                            "committed model stream could not be materialized"
                        )
                    )
                    return False
                handle._set_result(result)
                return True

            def reconciliation_blocked(
                diagnostic_code: str,
                *,
                error: BaseException | None = None,
            ) -> ModelStreamCompletion:
                handle._set_result_error(
                    error
                    or ModelStreamMaterializationError(
                        "model stream requires session reopen reconciliation"
                    )
                )
                return ModelStreamCompletion(
                    resolved_model_call_id=call_id,
                    terminal_outcome="reconciliation_blocked",
                    committed_events=tuple(committed),
                    diagnostic_code=diagnostic_code,
                )

            async def commit_stable_terminal(
                terminal_events: tuple[AgentEvent, ...],
            ) -> tuple[AgentEvent, ...]:
                """Retry a confirmed-NONE terminal write without changing facts."""

                candidates = tuple(
                    freeze_event_write_candidate(event) for event in terminal_events
                )
                guard = build_model_stream_terminal_commit_guard(
                    start_bundle,
                    expected_last_semantic_event_id=last_semantic_event_id,
                    semantic_item_count=semantic_item_count,
                )
                while True:
                    try:
                        if live_semantic_cursor is None:
                            raise RuntimeError(
                                "model terminal commit lacks its live semantic cursor"
                            )
                        return record_commit(
                            await commit_port.commit_terminal(
                                candidates,
                                guard=guard,
                                live_cursor=live_semantic_cursor,
                            )
                        )
                    except ModelStreamCommitNotCommitted:
                        # NONE is retryable, but only with the same frozen bytes.
                        try:
                            await asyncio.sleep(0.01)
                        except asyncio.CancelledError:
                            task = asyncio.current_task()
                            if task is not None:
                                task.uncancel()
                    except asyncio.CancelledError:
                        # Task cancellation cannot abandon a committed Start.
                        task = asyncio.current_task()
                        if task is not None:
                            task.uncancel()

            start_event = ModelCallStartEvent(
                id=recovery_plan.model_call_start_event_id,
                **event_context.event_fields(),
                resolved_call=call.fact,
                context_id=context.context_id or "",
                model_call_index=context.model_call_index,
                recovery_plan=recovery_plan,
            )
            start_batch = (
                *start_bundle.companion_candidates,
                freeze_event_write_candidate(start_event),
            )
            try:
                stored_start_batch = record_commit(
                    await commit_port.commit_start(
                        start_batch,
                        guard=build_model_stream_start_commit_guard(start_bundle),
                    )
                )
                committed_start = next(
                    event
                    for event in stored_start_batch
                    if isinstance(event, ModelCallStartEvent)
                )
                live_semantic_cursor = handle.install_live_semantic_cursor(
                    committed_start
                )
            except BaseException as exc:
                if runtime_session.reconciliation_required:
                    return reconciliation_blocked(
                        "model_stream_start_commit_reconciliation_required",
                        error=exc,
                    )
                raise

            try:
                execution = call.target.transport.open_stream(
                    call=call,
                    context=context,
                )
            except BaseException:
                # A production sanitizing transport must contain every raw
                # provider/SDK failure. Reaching this guard is an architecture
                # fault, so do not retain or serialize the original exception.
                self._registry.latch_untrusted(
                    call.target.transport,
                    reason_code="public_transport_open_stream_fault",
                )
                terminal_events = self._terminal_batch(
                    call=call,
                    event_context=event_context,
                    recovery_plan=recovery_plan,
                    validation_estimate=validation.estimate.total_input_tokens,
                    outcome="runtime_error",
                    provider_dispatch_status="not_started",
                    usage_report=None,
                    runtime_session=runtime_session,
                    reservation=rollout_reservation,
                )
                try:
                    await commit_stable_terminal(terminal_events)
                except BaseException as exc:
                    return reconciliation_blocked(
                        "public_transport_open_stream_fault_terminal_unconfirmed",
                        error=exc,
                    )
                if not await materialize_terminal_result():
                    return reconciliation_blocked(
                        "model_stream_materialization_failed"
                    )
                return ModelStreamCompletion(
                    resolved_model_call_id=call_id,
                    terminal_outcome="runtime_error",
                    committed_events=tuple(committed),
                    diagnostic_code="public_transport_open_stream_fault",
                )
            terminal_draft: ProviderTransportTerminalDraft | None = None
            diagnostic_code: str | None = None
            pending_semantic_events: list[AgentEvent] = []
            pending_semantic_chars = 0
            pending_semantic_started_at: float | None = None

            async def flush_semantic_events() -> None:
                nonlocal semantic_item_count
                nonlocal last_semantic_event_id
                nonlocal pending_semantic_chars
                nonlocal pending_semantic_started_at
                if not pending_semantic_events:
                    return
                if live_semantic_cursor is None:
                    raise RuntimeError(
                        "model semantic commit lacks its live semantic cursor"
                    )
                batch = tuple(pending_semantic_events)
                record_commit(
                    await commit_port.commit_semantic(
                        tuple(freeze_event_write_candidate(event) for event in batch),
                        guard=ModelStreamSemanticCommitGuard(
                            resolved_model_call_id=call_id,
                            model_call_start_event_id=start_event.id,
                            first_transport_sequence_index=semantic_item_count,
                            semantic_item_count=len(batch),
                            expected_previous_semantic_event_id=(
                                last_semantic_event_id
                            ),
                        ),
                        live_cursor=live_semantic_cursor,
                    )
                )
                semantic_item_count += len(batch)
                last_semantic_event_id = batch[-1].id
                pending_semantic_events.clear()
                pending_semantic_chars = 0
                pending_semantic_started_at = None

            try:
                while terminal_draft is None:
                    read_task = asyncio.create_task(execution.read_next())
                    operation_id = handle.register_physical_operation(read_task)
                    cancel_task: asyncio.Task[str] | None = None
                    flush_task: asyncio.Task[None] | None = None
                    try:
                        while True:
                            cancel_task = asyncio.create_task(
                                handle.wait_cancellation_requested()
                            )
                            flush_task = None
                            waiters: list[asyncio.Task[object]] = [
                                read_task,
                                cancel_task,
                            ]
                            if pending_semantic_started_at is not None:
                                flush_delay = max(
                                    0.0,
                                    pending_semantic_started_at
                                    + _SEMANTIC_BATCH_MAX_AGE_SECONDS
                                    - monotonic(),
                                )
                                flush_task = asyncio.create_task(
                                    asyncio.sleep(flush_delay)
                                )
                                waiters.append(flush_task)
                            done, _ = await asyncio.wait(
                                waiters,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if (
                                flush_task is not None
                                and flush_task in done
                                and read_task not in done
                                and cancel_task not in done
                            ):
                                cancel_task.cancel()
                                await asyncio.gather(
                                    cancel_task, return_exceptions=True
                                )
                                await flush_semantic_events()
                                continue
                            if cancel_task in done:
                                reason = cancel_task.result()
                                await execution.request_cancel(reason=reason)
                                if not read_task.done():
                                    read_task.cancel()
                                await asyncio.gather(
                                    read_task, return_exceptions=True
                                )
                                # The first cancel/close may race an in-flight
                                # ``anext()``. Retry the idempotent close after
                                # that exact read has physically returned.
                                await execution.aclose()
                                physical = (
                                    await execution.wait_physical_completion()
                                )
                                if (
                                    physical.status
                                    is not ProviderTransportPhysicalCompletionStatus.COMPLETED
                                ):
                                    runtime_session.latch_event_commit_outcome_unknown()
                                    return reconciliation_blocked(
                                        physical.diagnostic_code
                                        or "provider_physical_completion_untrusted"
                                    )
                                terminal_events = self._terminal_batch(
                                    call=call,
                                    event_context=event_context,
                                    recovery_plan=recovery_plan,
                                    validation_estimate=validation.estimate.total_input_tokens,
                                    outcome="cancelled",
                                    usage_report=None,
                                    runtime_session=runtime_session,
                                    reservation=rollout_reservation,
                                )
                                await commit_stable_terminal(terminal_events)
                                if not await materialize_terminal_result():
                                    return reconciliation_blocked(
                                        "model_stream_materialization_failed"
                                    )
                                return ModelStreamCompletion(
                                    resolved_model_call_id=call_id,
                                    terminal_outcome="cancelled",
                                    committed_events=tuple(committed),
                                    diagnostic_code="model_stream_cancelled",
                                )
                            cancel_task.cancel()
                            await asyncio.gather(
                                cancel_task, return_exceptions=True
                            )
                            item = read_task.result()
                            break
                    except asyncio.CancelledError:
                        # A naked worker cancellation is an architecture fault,
                        # not authority to release an in-flight provider read.
                        task = asyncio.current_task()
                        if task is not None:
                            task.uncancel()
                        if cancel_task is not None:
                            cancel_task.cancel()
                            await asyncio.gather(
                                cancel_task, return_exceptions=True
                            )
                        await execution.request_cancel(reason="host_teardown")
                        if not read_task.done():
                            read_task.cancel()
                        await asyncio.gather(read_task, return_exceptions=True)
                        # request_cancel may race an active anext(); retry the
                        # idempotent close only after that exact read has exited.
                        await execution.aclose()
                        physical = await execution.wait_physical_completion()
                        if (
                            physical.status
                            is not ProviderTransportPhysicalCompletionStatus.COMPLETED
                        ):
                            runtime_session.latch_event_commit_outcome_unknown()
                            return reconciliation_blocked(
                                physical.diagnostic_code
                                or "provider_physical_completion_untrusted"
                            )
                        terminal_events = self._terminal_batch(
                            call=call,
                            event_context=event_context,
                            recovery_plan=recovery_plan,
                            validation_estimate=validation.estimate.total_input_tokens,
                            outcome="runtime_error",
                            usage_report=None,
                            runtime_session=runtime_session,
                            reservation=rollout_reservation,
                        )
                        await commit_stable_terminal(terminal_events)
                        if not await materialize_terminal_result():
                            return reconciliation_blocked(
                                "model_stream_materialization_failed"
                            )
                        return ModelStreamCompletion(
                            resolved_model_call_id=call_id,
                            terminal_outcome="runtime_error",
                            committed_events=tuple(committed),
                            diagnostic_code=(
                                "model_stream_worker_cancelled_without_intent"
                            ),
                        )
                    finally:
                        if flush_task is not None:
                            flush_task.cancel()
                            await asyncio.gather(
                                flush_task, return_exceptions=True
                            )
                        if read_task.done():
                            handle.complete_physical_operation(operation_id)
                    if item is None:
                        raise LLMTransportContractError(
                            "provider stream ended without terminal draft",
                            reason_code="provider_terminal_draft_missing",
                        )
                    if isinstance(item, ProviderTransportTerminalDraft):
                        await flush_semantic_events()
                        if item.semantic_item_count != semantic_item_count:
                            raise LLMTransportContractError(
                                "provider terminal semantic count mismatch",
                                reason_code="provider_semantic_item_count_mismatch",
                            )
                        physical = await execution.wait_physical_completion()
                        if (
                            physical.status
                            is not ProviderTransportPhysicalCompletionStatus.COMPLETED
                        ):
                            runtime_session.latch_event_commit_outcome_unknown()
                            return reconciliation_blocked(
                                physical.diagnostic_code
                                or "provider_physical_completion_untrusted"
                            )
                        terminal_draft = item
                        continue
                    semantic_event = self._semantic_event_from_draft(
                        call=call,
                        event_context=event_context,
                        model_call_start_event_id=start_event.id,
                        draft=item,
                    )
                    pending_semantic_events.append(semantic_event)
                    pending_semantic_chars += _semantic_event_content_chars(
                        semantic_event
                    )
                    if pending_semantic_started_at is None:
                        pending_semantic_started_at = monotonic()
                    structural = not isinstance(
                        semantic_event,
                        (
                            TextBlockDeltaEvent,
                            ThinkingBlockDeltaEvent,
                            DataBlockDeltaEvent,
                            ToolCallDeltaEvent,
                        ),
                    )
                    if (
                        structural
                        or len(pending_semantic_events)
                        >= _SEMANTIC_BATCH_MAX_EVENTS
                        or pending_semantic_chars >= _SEMANTIC_BATCH_MAX_CHARS
                        or monotonic() - pending_semantic_started_at
                        >= _SEMANTIC_BATCH_MAX_AGE_SECONDS
                    ):
                        await flush_semantic_events()

                terminal_events = self._terminal_batch(
                    call=call,
                    event_context=event_context,
                    recovery_plan=recovery_plan,
                    validation_estimate=validation.estimate.total_input_tokens,
                    outcome=terminal_draft.outcome,
                    usage_report=TransportUsageReport(
                        usage_status=terminal_draft.usage_status,
                        usage=terminal_draft.usage,
                        reported_model_id=terminal_draft.reported_model_id,
                    ),
                    runtime_session=runtime_session,
                    reservation=rollout_reservation,
                )
                await commit_stable_terminal(terminal_events)
                if not await materialize_terminal_result():
                    return reconciliation_blocked(
                        "model_stream_materialization_failed"
                    )
                if terminal_draft.outcome == "provider_error":
                    diagnostic_code = "provider_error"
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    # Registry tasks are not the public cancellation protocol.
                    # A naked task cancellation is an architecture failure.
                    diagnostic_code = "model_stream_worker_cancelled_without_intent"
                else:
                    diagnostic_code = "model_stream_runtime_error"
                if runtime_session.reconciliation_required:
                    return reconciliation_blocked(
                        diagnostic_code,
                        error=exc,
                    )
                if start_committed and not terminal_committed:
                    terminal_events = self._terminal_batch(
                        call=call,
                        event_context=event_context,
                        recovery_plan=recovery_plan,
                        validation_estimate=validation.estimate.total_input_tokens,
                        outcome="runtime_error",
                        usage_report=None,
                        runtime_session=runtime_session,
                        reservation=rollout_reservation,
                    )
                    await commit_stable_terminal(terminal_events)
                    if not await materialize_terminal_result():
                        return reconciliation_blocked(
                            "model_stream_materialization_failed"
                        )
                if not start_committed:
                    raise
                return ModelStreamCompletion(
                    resolved_model_call_id=call_id,
                    terminal_outcome="runtime_error",
                    committed_events=tuple(committed),
                    diagnostic_code=diagnostic_code,
                )

            if not terminal_committed:
                raise LLMTransportContractError(
                    "model stream ended without a committed terminal batch",
                    reason_code="model_stream_lifecycle_incomplete",
                )
            return ModelStreamCompletion(
                resolved_model_call_id=call_id,
                terminal_outcome=terminal_draft.outcome,
                committed_events=tuple(committed),
                diagnostic_code=diagnostic_code,
            )

        return execution_registry.install_and_start(
            handle_id=handle_id,
            run_id=event_context.run_id,
            resolved_model_call_id=call.fact.resolved_model_call_id,
            subscription_start_sequence=(
                runtime_session.long_horizon_state_store.through_sequence
            ),
            worker=worker,
        )

    @staticmethod
    def _semantic_event_from_draft(
        *,
        call: ResolvedModelCall,
        event_context: EventContext,
        model_call_start_event_id: str,
        draft: ProviderTransportSemanticDraft,
    ) -> AgentEvent:
        attribution_payload = {
            "schema_version": "model_stream_semantic_attribution.v1",
            "resolved_model_call_id": call.fact.resolved_model_call_id,
            "model_call_start_event_id": model_call_start_event_id,
            "transport_sequence_index": draft.transport_sequence_index,
            "draft_schema_version": draft.schema_version,
            "draft_kind": draft.draft_kind,
            "draft_fingerprint": draft.draft_fingerprint,
        }
        attribution = ModelStreamSemanticAttributionFact(
            **attribution_payload,
            attribution_fingerprint=sha256_fingerprint(
                "model-stream-semantic-attribution:v1", attribution_payload
            ),
        )
        event_id = (
            f"model_semantic:{call.fact.resolved_model_call_id}:"
            f"{draft.transport_sequence_index}:{draft.draft_fingerprint[7:23]}"
        )
        common = {
            "id": event_id,
            **event_context.event_fields(),
            "model_stream_attribution": attribution,
        }
        if isinstance(draft, ProviderTextBlockStartDraft):
            return TextBlockStartEvent(**common, block_id=draft.block_id)
        if isinstance(draft, ProviderTextBlockDeltaDraft):
            return TextBlockDeltaEvent(
                **common, block_id=draft.block_id, delta=draft.delta
            )
        if isinstance(draft, ProviderTextBlockEndDraft):
            return TextBlockEndEvent(**common, block_id=draft.block_id)
        if isinstance(draft, ProviderThinkingBlockStartDraft):
            return ThinkingBlockStartEvent(**common, block_id=draft.block_id)
        if isinstance(draft, ProviderThinkingBlockDeltaDraft):
            return ThinkingBlockDeltaEvent(
                **common, block_id=draft.block_id, delta=draft.delta
            )
        if isinstance(draft, ProviderThinkingBlockEndDraft):
            return ThinkingBlockEndEvent(**common, block_id=draft.block_id)
        if isinstance(draft, ProviderDataBlockStartDraft):
            return DataBlockStartEvent(
                **common, block_id=draft.block_id, media_type=draft.media_type
            )
        if isinstance(draft, ProviderDataBlockDeltaDraft):
            return DataBlockDeltaEvent(
                **common,
                block_id=draft.block_id,
                media_type=draft.media_type,
                data=draft.data,
            )
        if isinstance(draft, ProviderDataBlockEndDraft):
            return DataBlockEndEvent(**common, block_id=draft.block_id)
        if isinstance(draft, ProviderToolCallStartDraft):
            return ToolCallStartEvent(
                **common,
                tool_call_id=draft.tool_call_id,
                tool_call_name=draft.tool_call_name,
            )
        if isinstance(draft, ProviderToolCallDeltaDraft):
            return ToolCallDeltaEvent(
                **common, tool_call_id=draft.tool_call_id, delta=draft.delta
            )
        if isinstance(draft, ProviderToolCallEndDraft):
            return ToolCallEndEvent(**common, tool_call_id=draft.tool_call_id)
        if isinstance(draft, ProviderErrorDraft):
            return ProviderModelStreamErrorEvent(**common, error=draft.error)
        raise TypeError(f"unsupported provider semantic draft: {type(draft).__name__}")

    def _terminal_batch(
        self,
        *,
        call: ResolvedModelCall,
        event_context: EventContext,
        recovery_plan: ModelStreamRecoveryPlanFact,
        validation_estimate: int,
        outcome: str,
        provider_dispatch_status: str = "dispatched",
        usage_report: TransportUsageReport | None,
        runtime_session: "RuntimeSession",
        reservation: RolloutReservationFact | None,
    ) -> tuple[AgentEvent, ...]:
        usage_report = usage_report or TransportUsageReport(
            usage_status="missing", usage=None
        )
        model_end = ModelCallEndEvent(
            id=recovery_plan.stable_model_call_end_event_id,
            **event_context.event_fields(),
            resolved_model_call_id=call.fact.resolved_model_call_id,
            target_fingerprint=call.target.fact.target_fingerprint,
            reported_model_id=usage_report.reported_model_id,
            outcome=outcome,
            provider_dispatch_status=provider_dispatch_status,
            usage_status=usage_report.usage_status,
            usage=usage_report.usage,
            estimated_input_tokens=validation_estimate,
            diagnostics=usage_report.provider_diagnostics,
        )
        events: list[AgentEvent] = [model_end]
        if reservation is not None:
            settlement = self._build_model_settlement_event(
                event_context=event_context,
                runtime_session=runtime_session,
                reservation=reservation,
                model_end=model_end,
            )
            if settlement.id != recovery_plan.stable_settlement_event_id:
                raise RuntimeError("model settlement stable identity mismatch")
            events.append(settlement)
        if recovery_plan.stable_reply_end_event_id is not None:
            events.append(
                ReplyEndEvent(
                    id=recovery_plan.stable_reply_end_event_id,
                    **event_context.event_fields(),
                    model_terminal_outcome=outcome,
                )
            )
        return tuple(events)

    @staticmethod
    def _build_model_settlement_event(
        *,
        event_context: EventContext,
        runtime_session: "RuntimeSession",
        reservation: RolloutReservationFact,
        model_end: ModelCallEndEvent,
    ) -> RolloutBudgetReservationSettledEvent:
        from pulsara_agent.runtime.long_horizon.accounting import (
            resolve_run_rollout_binding,
        )

        binding = resolve_run_rollout_binding(
            runtime_session,
            run_id=event_context.run_id,
        )
        account = binding.account
        if account.account_id != reservation.account_id:
            raise RuntimeError("model settlement cannot rebind its rollout quote")
        return build_model_reservation_settlement_event(
            event_context=event_context,
            account=account,
            reservation=reservation,
            model_end=model_end,
        )
