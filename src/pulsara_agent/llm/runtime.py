"""Resolved-target LLM runtime."""

from __future__ import annotations

import asyncio
from time import monotonic_ns
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ModelCallTerminalProjectionCommittedEvent,
    ReplyEndEvent,
    RolloutBudgetReservationSettledEvent,
)
from pulsara_agent.llm.drafts import (
    ProviderErrorDraft,
    ProviderTransportTerminalDraft,
    SanitizedProviderSemanticEnvelope,
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
from pulsara_agent.llm.coalescing import (
    ModelStreamCoalescingCoordinator,
    ModelStreamInputSignalKind,
    ModelStreamReadySignal,
)
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
    ModelStreamSemanticCommitMeasurementFact,
    ModelStreamSegmentSealReason,
    ResolvedModelTargetFact,
    sha256_fingerprint,
)
from pulsara_agent.primitives.run_boundary import (
    ModelStreamRecoveryPlanFact,
)
from pulsara_agent.primitives.long_horizon import (
    RolloutReservationFact,
)
from pulsara_agent.primitives.authority_materialization import PhysicalOperationKind
from pulsara_agent.llm.errors import LLMTransportContractError
from pulsara_agent.llm.execution import (
    ModelStreamCompletion,
    ModelStreamExecutionHandle,
    ModelStreamExecutionRegistry,
)
from pulsara_agent.llm.lifecycle import (
    ModelLifecycleStartCommitBundle,
    build_active_run_monitor_start_companions,
    validate_model_lifecycle_start_bundle,
)
from pulsara_agent.llm.materialize import (
    ModelStreamMaterializationError,
    materialize_committed_model_call_result_from_terminal_projection,
)
from pulsara_agent.llm.sanitizing_transport import (
    ProviderTransportPhysicalCompletion,
    ProviderTransportPhysicalCompletionStatus,
)
from pulsara_agent.llm.terminal_projection import (
    ModelTerminalProjectionReducer,
    PreparedModelTerminalProjection,
    bind_model_terminal_projection_to_session,
    build_model_stream_semantic_commit_measurement,
    persist_model_terminal_projection,
    TerminalProjectionPersistenceContractError,
)
from pulsara_agent.llm.accounting import (
    build_model_reservation_settlement_event,
)
from pulsara_agent.llm.segment import (
    MODEL_STREAM_SEGMENT_POLICY,
    ModelStreamSegmentAccumulator,
)

if TYPE_CHECKING:
    from pulsara_agent.runtime.provider_input.planner import (
        PreparedProviderInputStartBundle,
    )
    from pulsara_agent.runtime.session import RuntimeSession


class LLMRuntime:
    def __init__(
        self,
        *,
        config: LLMConfig,
        registry: LLMTransportRegistry,
    ) -> None:
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
        model_burst_contract = runtime_session.authority_materialization_contracts.burst_registry.unique_binding_for_operation(
            PhysicalOperationKind.MODEL_CALL
        ).contract
        call_id = call.fact.resolved_model_call_id
        handle_id = f"model_stream:{call_id}:{uuid4().hex}"

        async def worker(handle: ModelStreamExecutionHandle) -> ModelStreamCompletion:
            await handle.wait_until_activated()
            committed: list[AgentEvent] = []
            start_committed = False
            terminal_committed = False
            source_item_count = 0
            source_accumulator = sha256_fingerprint(
                "model-stream-sanitized-source:v2", "empty"
            )
            durable_event_count = 0
            last_semantic_event_id: str | None = None
            live_semantic_cursor = None
            terminal_projection_reducer: ModelTerminalProjectionReducer | None = None
            semantic_commit_measurements: list[
                ModelStreamSemanticCommitMeasurementFact
            ] = []

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
                    ends = tuple(
                        event
                        for event in committed
                        if isinstance(event, ModelCallEndEvent)
                        and event.resolved_model_call_id == call_id
                    )
                    if len(ends) != 1:
                        raise ModelStreamMaterializationError(
                            "committed model stream lacks one terminal projection"
                        )
                    document = (
                        runtime_session.transcript_projection_document_registry.resolve(
                            ends[0].terminal_projection.projection_reference
                        )
                    )
                    result = materialize_committed_model_call_result_from_terminal_projection(
                        tuple(committed),
                        resolved_model_call_id=call_id,
                        runtime_session_id=runtime_session.runtime_session_id,
                        document=document,
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
                    source_item_count=source_item_count,
                    source_accumulator=source_accumulator,
                    durable_event_count=durable_event_count,
                    model_stream_measurement_fingerprint=(
                        runtime_session.transcript_projection_document_registry.resolve(
                            next(
                                event
                                for event in terminal_events
                                if isinstance(
                                    event,
                                    ModelCallTerminalProjectionCommittedEvent,
                                )
                            ).projection_reference
                        ).source_fact.stream_settlement_measurement.measurement_fingerprint
                    ),
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

            provider_input_start = start_bundle.provider_input_start_bundle
            if provider_input_start is None:
                raise RuntimeError(
                    "model lifecycle Start lacks committed provider-input owner"
                )
            start_event = ModelCallStartEvent(
                id=recovery_plan.model_call_start_event_id,
                **event_context.event_fields(),
                resolved_call=call.fact,
                context_id=context.context_id or "",
                model_call_index=context.model_call_index,
                recovery_plan=recovery_plan,
                governance_input_attribution=(
                    start_bundle.governance_input_attribution
                ),
                provider_input_reference=provider_input_start.committed_reference,
                active_run_monitor_delivery=(
                    start_bundle.active_run_monitor_delivery
                ),
            )
            active_run_monitor_companions = (
                build_active_run_monitor_start_companions(
                    bundle=start_bundle,
                    start_event=start_event,
                    runtime_session=runtime_session,
                )
            )
            start_batch = (
                *start_bundle.companion_candidates,
                *(
                    freeze_event_write_candidate(event)
                    for event in active_run_monitor_companions
                ),
                freeze_event_write_candidate(start_event),
            )
            try:
                await commit_port.ensure_physical_headroom()
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
                terminal_projection_reducer = ModelTerminalProjectionReducer(
                    runtime_session_id=runtime_session.runtime_session_id,
                    start_event=committed_start,
                    contracts=runtime_session.terminal_projection_contracts,
                    model_stream_semantic_domain_contract_fingerprint=(
                        runtime_session.authority_materialization_contracts.event_domain.contract.transcript_semantic_domain_contract_fingerprint
                    ),
                    segment_policy_contract_fingerprint=(
                        MODEL_STREAM_SEGMENT_POLICY.contract_fingerprint
                    ),
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
                if terminal_projection_reducer is None:
                    raise RuntimeError("model terminal projection reducer is missing")
                terminal_events = await self._prepare_terminal_batch(
                    call=call,
                    event_context=event_context,
                    recovery_plan=recovery_plan,
                    validation_estimate=validation.estimate.total_input_tokens,
                    outcome="runtime_error",
                    provider_dispatch_status="not_started",
                    usage_report=None,
                    runtime_session=runtime_session,
                    reservation=rollout_reservation,
                    projection_reducer=terminal_projection_reducer,
                    semantic_commit_measurements=tuple(semantic_commit_measurements),
                    physical_accounting_mode=(
                        "accounted"
                        if runtime_session.materialization_account_store.snapshot()
                        is not None
                        else "unbootstrapped_test"
                    ),
                    provider_input_start_bundle=(
                        start_bundle.provider_input_start_bundle
                    ),
                )
                try:
                    await commit_stable_terminal(terminal_events)
                except BaseException as exc:
                    return reconciliation_blocked(
                        "public_transport_open_stream_fault_terminal_unconfirmed",
                        error=exc,
                    )
                if not await materialize_terminal_result():
                    return reconciliation_blocked("model_stream_materialization_failed")
                return ModelStreamCompletion(
                    resolved_model_call_id=call_id,
                    terminal_outcome="runtime_error",
                    committed_events=tuple(committed),
                    diagnostic_code="public_transport_open_stream_fault",
                )
            terminal_draft: ProviderTransportTerminalDraft | None = None
            diagnostic_code: str | None = None
            semantic_commit_batch_count = 0
            coordinator = ModelStreamCoalescingCoordinator(
                transport=execution,
                segment_accumulator=ModelStreamSegmentAccumulator(
                    resolved_model_call_id=call_id,
                    model_call_start_event_id=start_event.id,
                    context=event_context,
                ),
                arbiter=handle.input_arbiter,
            )
            handle.install_coalescing_owner(coordinator)

            async def flush_semantic_events() -> None:
                nonlocal source_item_count
                nonlocal source_accumulator
                nonlocal durable_event_count
                nonlocal last_semantic_event_id
                nonlocal semantic_commit_batch_count
                batch = coordinator.batch.freeze()
                if not batch:
                    return
                if (
                    semantic_commit_batch_count
                    >= model_burst_contract.max_commit_batches
                ):
                    raise LLMTransportContractError(
                        "model semantic commit batch limit exceeded",
                        reason_code="provider_semantic_commit_batch_limit_exceeded",
                    )
                if live_semantic_cursor is None:
                    raise RuntimeError(
                        "model semantic commit lacks its live semantic cursor"
                    )
                first_attribution = batch[0].event.model_stream_attribution  # type: ignore[union-attr]
                last_attribution = batch[-1].event.model_stream_attribution  # type: ignore[union-attr]
                guard = ModelStreamSemanticCommitGuard(
                    resolved_model_call_id=call_id,
                    model_call_start_event_id=start_event.id,
                    first_transport_sequence_index=(
                        first_attribution.source_span.first_transport_sequence_index
                    ),
                    source_item_count=sum(item.source_item_count for item in batch),
                    source_accumulator_before=(
                        first_attribution.source_span.source_accumulator_before
                    ),
                    source_accumulator_after=(
                        last_attribution.source_span.source_accumulator_after
                    ),
                    first_durable_semantic_event_index=(
                        first_attribution.durable_semantic_event_index
                    ),
                    durable_event_count=len(batch),
                    expected_previous_semantic_event_id=last_semantic_event_id,
                )
                while True:
                    try:
                        confirmed_semantic = await commit_port.commit_semantic(
                            tuple(item.candidate for item in batch),
                            guard=guard,
                            live_cursor=live_semantic_cursor,
                        )
                        stored_semantic = record_commit(confirmed_semantic)
                        break
                    except ModelStreamCommitNotCommitted:
                        await asyncio.sleep(0.01)
                if terminal_projection_reducer is None:
                    raise RuntimeError("model semantic projection reducer is missing")
                terminal_projection_reducer.apply_committed(stored_semantic)
                commit_measurement = build_model_stream_semantic_commit_measurement(
                    runtime_session_id=runtime_session.runtime_session_id,
                    commit_batch_index=len(semantic_commit_measurements),
                    committed_semantic_events=stored_semantic,
                    accounting_events=confirmed_semantic.accounting_events,
                )
                if commit_measurement is not None:
                    semantic_commit_measurements.append(commit_measurement)
                elif (
                    runtime_session.materialization_account_store.snapshot() is not None
                ):
                    raise RuntimeError(
                        "accounted model semantic commit lost its measurement"
                    )
                semantic_commit_batch_count += 1
                source_item_count = live_semantic_cursor.confirmed_source_item_count
                source_accumulator = live_semantic_cursor.confirmed_source_accumulator
                durable_event_count = live_semantic_cursor.confirmed_durable_event_count
                last_semantic_event_id = live_semantic_cursor.last_semantic_event_id
                coordinator.confirm_current_batch_full()

            async def flush_owned_semantic_events(*, force: bool) -> None:
                while coordinator.batch.event_count:
                    if (
                        not force
                        and not coordinator.batch.must_commit()
                        and not coordinator.has_pending_candidates
                    ):
                        return
                    await flush_semantic_events()

            async def seal_and_flush(reason: ModelStreamSegmentSealReason) -> None:
                coordinator.seal(reason)
                await flush_owned_semantic_events(force=True)

            async def read_with_stamp():
                item = await execution.read_next()
                return (
                    coordinator.arbiter.stamp(
                        observed_monotonic_ns=(
                            item.accepted_at_monotonic_ns
                            if isinstance(
                                item,
                                SanitizedProviderSemanticEnvelope,
                            )
                            else None
                        )
                    ),
                    item,
                )

            async def cancellation_with_stamp():
                reason, stamp = await handle.wait_cancellation_requested()
                return stamp, reason

            async def drain_transport_after_fault(
                *,
                reason: Literal["user_stop", "host_teardown"],
                outstanding: SanitizedProviderSemanticEnvelope | None,
                read_task: asyncio.Task[object] | None,
                operation_id: str | None,
            ) -> ProviderTransportPhysicalCompletion:
                if outstanding is not None:
                    coordinator.discard_unadopted(outstanding)
                discarded_envelope_id = (
                    outstanding.envelope_id if outstanding is not None else None
                )
                if read_task is not None:
                    if not read_task.done():
                        read_task.cancel()
                    late_result = await asyncio.gather(
                        read_task, return_exceptions=True
                    )
                    late = late_result[0]
                    if isinstance(late, tuple) and len(late) == 2:
                        late_item = late[1]
                        if (
                            isinstance(late_item, SanitizedProviderSemanticEnvelope)
                            and late_item.envelope_id != discarded_envelope_id
                        ):
                            coordinator.discard_unadopted(late_item)
                    if operation_id is not None:
                        handle.complete_physical_operation(operation_id)
                await execution.request_cancel(reason=reason)
                await execution.aclose()
                return await execution.wait_physical_completion()

            async def close_after_transport_stop(
                *,
                reason: Literal["user_stop", "host_teardown"],
                terminal_outcome: Literal["cancelled", "runtime_error"],
                completion_diagnostic_code: str,
                outstanding: SanitizedProviderSemanticEnvelope | None,
                read_task: asyncio.Task[object] | None,
                operation_id: str | None,
            ) -> ModelStreamCompletion:
                await seal_and_flush(ModelStreamSegmentSealReason.CANCELLATION_BOUNDARY)
                physical = await drain_transport_after_fault(
                    reason=reason,
                    outstanding=outstanding,
                    read_task=read_task,
                    operation_id=operation_id,
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
                if terminal_projection_reducer is None:
                    raise RuntimeError("model terminal projection reducer is missing")
                terminal_events = await self._prepare_terminal_batch(
                    call=call,
                    event_context=event_context,
                    recovery_plan=recovery_plan,
                    validation_estimate=validation.estimate.total_input_tokens,
                    outcome=terminal_outcome,
                    usage_report=None,
                    runtime_session=runtime_session,
                    reservation=rollout_reservation,
                    projection_reducer=terminal_projection_reducer,
                    semantic_commit_measurements=tuple(semantic_commit_measurements),
                    physical_accounting_mode=(
                        "accounted"
                        if runtime_session.materialization_account_store.snapshot()
                        is not None
                        else "unbootstrapped_test"
                    ),
                    provider_input_start_bundle=(
                        start_bundle.provider_input_start_bundle
                    ),
                )
                await commit_stable_terminal(terminal_events)
                if not await materialize_terminal_result():
                    return reconciliation_blocked("model_stream_materialization_failed")
                return ModelStreamCompletion(
                    resolved_model_call_id=call_id,
                    terminal_outcome=terminal_outcome,
                    committed_events=tuple(committed),
                    diagnostic_code=completion_diagnostic_code,
                )

            read_task: asyncio.Task[object] | None = None
            read_operation_id: str | None = None
            cancel_task: asyncio.Task[object] | None = None
            deadline_task: asyncio.Task[None] | None = None
            pending_unadopted_envelope: SanitizedProviderSemanticEnvelope | None = None
            try:
                read_task = asyncio.create_task(read_with_stamp())
                read_operation_id = handle.register_physical_operation(read_task)
                cancel_task = asyncio.create_task(cancellation_with_stamp())
                while terminal_draft is None:
                    deadline_ns = coordinator.oldest_unconfirmed_deadline_monotonic_ns
                    deadline_task = None
                    waiters = [read_task, cancel_task]
                    if deadline_ns is not None:
                        delay = max(
                            0.0,
                            (deadline_ns - monotonic_ns()) / 1_000_000_000,
                        )
                        deadline_task = asyncio.create_task(asyncio.sleep(delay))
                        waiters.append(deadline_task)
                    done, _ = await asyncio.wait(
                        tuple(task for task in waiters if task is not None),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    ready: list[ModelStreamReadySignal[object]] = []
                    ready_read_item: object | None = None
                    if deadline_task is not None and deadline_task in done:
                        ready.append(
                            ModelStreamReadySignal(
                                kind=ModelStreamInputSignalKind.DEADLINE,
                                stamp=None,
                                deadline_monotonic_ns=deadline_ns,
                                payload=None,
                            )
                        )
                    if read_task is not None and read_task in done:
                        read_stamp, ready_read_item = read_task.result()
                        pending_unadopted_envelope = (
                            ready_read_item
                            if isinstance(
                                ready_read_item, SanitizedProviderSemanticEnvelope
                            )
                            else None
                        )
                        ready.append(
                            ModelStreamReadySignal(
                                kind=ModelStreamInputSignalKind.READ,
                                stamp=read_stamp,
                                deadline_monotonic_ns=None,
                                payload=ready_read_item,
                            )
                        )
                    if cancel_task in done:
                        cancel_stamp, cancel_reason = cancel_task.result()
                        ready.append(
                            ModelStreamReadySignal(
                                kind=ModelStreamInputSignalKind.CANCEL,
                                stamp=cancel_stamp,
                                deadline_monotonic_ns=None,
                                payload=cancel_reason,
                            )
                        )

                    read_adopted = False
                    for signal in coordinator.arbiter.order_ready(tuple(ready)):
                        if signal.kind is ModelStreamInputSignalKind.DEADLINE:
                            await seal_and_flush(
                                ModelStreamSegmentSealReason.MAXIMUM_UNCONFIRMED_AGE
                            )
                            continue
                        if signal.kind is ModelStreamInputSignalKind.CANCEL:
                            unadopted = (
                                ready_read_item
                                if isinstance(
                                    ready_read_item,
                                    SanitizedProviderSemanticEnvelope,
                                )
                                and not read_adopted
                                else None
                            )
                            cancel_task.cancel()
                            if deadline_task is not None:
                                deadline_task.cancel()
                            return await close_after_transport_stop(
                                reason=signal.payload,  # type: ignore[arg-type]
                                terminal_outcome="cancelled",
                                completion_diagnostic_code="model_stream_cancelled",
                                outstanding=unadopted,
                                read_task=read_task,
                                operation_id=read_operation_id,
                            )

                        item = signal.payload
                        if read_operation_id is not None:
                            handle.complete_physical_operation(read_operation_id)
                            read_operation_id = None
                        read_task = None
                        if item is None:
                            raise LLMTransportContractError(
                                "provider stream ended without terminal draft",
                                reason_code="provider_terminal_draft_missing",
                            )
                        if isinstance(item, ProviderTransportTerminalDraft):
                            await seal_and_flush(
                                ModelStreamSegmentSealReason.TERMINAL_BOUNDARY
                            )
                            if (
                                item.semantic_item_count != source_item_count
                                or item.semantic_source_accumulator
                                != source_accumulator
                            ):
                                raise LLMTransportContractError(
                                    "provider terminal source receipt mismatch",
                                    reason_code="provider_semantic_source_mismatch",
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
                            break
                        if not isinstance(item, SanitizedProviderSemanticEnvelope):
                            raise LLMTransportContractError(
                                "provider transport returned an invalid stream item",
                                reason_code="provider_stream_item_invalid",
                            )
                        coordinator.adopt(item)
                        read_adopted = True
                        pending_unadopted_envelope = None
                        await flush_owned_semantic_events(force=False)
                        if isinstance(item.draft, ProviderErrorDraft):
                            await flush_owned_semantic_events(force=True)

                    if deadline_task is not None and not deadline_task.done():
                        deadline_task.cancel()
                        await asyncio.gather(deadline_task, return_exceptions=True)
                    if terminal_draft is None and read_task is None:
                        read_task = asyncio.create_task(read_with_stamp())
                        read_operation_id = handle.register_physical_operation(
                            read_task
                        )

                cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)

                if terminal_projection_reducer is None:
                    raise RuntimeError("model terminal projection reducer is missing")
                terminal_events = await self._prepare_terminal_batch(
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
                    projection_reducer=terminal_projection_reducer,
                    semantic_commit_measurements=tuple(semantic_commit_measurements),
                    physical_accounting_mode=(
                        "accounted"
                        if runtime_session.materialization_account_store.snapshot()
                        is not None
                        else "unbootstrapped_test"
                    ),
                    provider_input_start_bundle=(
                        start_bundle.provider_input_start_bundle
                    ),
                )
                await commit_stable_terminal(terminal_events)
                if not await materialize_terminal_result():
                    return reconciliation_blocked("model_stream_materialization_failed")
                if terminal_draft.outcome == "provider_error":
                    diagnostic_code = "provider_error"
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    # Registry tasks are not the public cancellation protocol.
                    # A naked task cancellation is an architecture failure, but
                    # it must still drain the exact physical transport operation.
                    task = asyncio.current_task()
                    if task is not None:
                        task.uncancel()
                    if cancel_task is not None:
                        cancel_task.cancel()
                        await asyncio.gather(cancel_task, return_exceptions=True)
                    if deadline_task is not None:
                        deadline_task.cancel()
                        await asyncio.gather(deadline_task, return_exceptions=True)
                    return await close_after_transport_stop(
                        reason="host_teardown",
                        terminal_outcome="runtime_error",
                        completion_diagnostic_code=(
                            "model_stream_worker_cancelled_without_intent"
                        ),
                        outstanding=pending_unadopted_envelope,
                        read_task=read_task,
                        operation_id=read_operation_id,
                    )
                else:
                    diagnostic_code = "model_stream_runtime_error"
                if cancel_task is not None:
                    cancel_task.cancel()
                    await asyncio.gather(cancel_task, return_exceptions=True)
                if deadline_task is not None:
                    deadline_task.cancel()
                    await asyncio.gather(deadline_task, return_exceptions=True)
                if runtime_session.reconciliation_required:
                    try:
                        physical = await drain_transport_after_fault(
                            reason="host_teardown",
                            outstanding=pending_unadopted_envelope,
                            read_task=read_task,
                            operation_id=read_operation_id,
                        )
                    except BaseException:
                        runtime_session.latch_event_commit_outcome_unknown()
                    else:
                        if (
                            physical.status
                            is not ProviderTransportPhysicalCompletionStatus.COMPLETED
                        ):
                            runtime_session.latch_event_commit_outcome_unknown()
                    return reconciliation_blocked(
                        diagnostic_code,
                        error=exc,
                    )
                if not start_committed:
                    raise
                return await close_after_transport_stop(
                    reason="host_teardown",
                    terminal_outcome="runtime_error",
                    completion_diagnostic_code=diagnostic_code,
                    outstanding=pending_unadopted_envelope,
                    read_task=read_task,
                    operation_id=read_operation_id,
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

        shadow_owner_id: str | None = handle_id
        try:
            model_burst = runtime_session.authority_materialization_contracts.burst_registry.unique_binding_for_operation(
                PhysicalOperationKind.MODEL_CALL
            ).contract
            runtime_session.authority_materialization_shadow.observe_candidate(
                owner_id=handle_id,
                contract=model_burst,
            )
        except Exception:
            # AP0 shadow is diagnostic-only. It must never become an accidental
            # admission gate before AP4 installs the durable reservation owner.
            shadow_owner_id = None

        async def observed_worker(
            handle: ModelStreamExecutionHandle,
        ) -> ModelStreamCompletion:
            try:
                return await worker(handle)
            finally:
                provider_bundle = start_bundle.provider_input_start_bundle
                if provider_bundle is not None:
                    try:
                        await runtime_session.provider_input_generation_coordinator.abandon_uncommitted_preparation(
                            provider_bundle.prepared_candidate.preparation_ownership.preparation_id,
                            reason="run_terminated_before_start",
                        )
                    except BaseException:
                        # A committed Start consumes the preparation and this is
                        # a no-op. Any unresolved/unknown pre-Start owner must
                        # remain durable and block teardown for exact recovery.
                        runtime_session.latch_event_commit_outcome_unknown()
                if shadow_owner_id is not None:
                    runtime_session.authority_materialization_shadow.release_candidate(
                        shadow_owner_id
                    )

        try:
            return execution_registry.install_and_start(
                handle_id=handle_id,
                run_id=event_context.run_id,
                resolved_model_call_id=call.fact.resolved_model_call_id,
                subscription_start_sequence=(
                    runtime_session.long_horizon_state_store.through_sequence
                ),
                worker=observed_worker,
            )
        except BaseException:
            if start_bundle.provider_input_start_bundle is not None:
                runtime_session.provider_input_generation_coordinator.reject_before_worker_start(
                    start_bundle.provider_input_start_bundle
                )
            if shadow_owner_id is not None:
                runtime_session.authority_materialization_shadow.release_candidate(
                    shadow_owner_id
                )
            raise

    async def _prepare_terminal_batch(
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
        projection_reducer: ModelTerminalProjectionReducer,
        semantic_commit_measurements: tuple[
            ModelStreamSemanticCommitMeasurementFact, ...
        ] = (),
        physical_accounting_mode: Literal[
            "accounted", "unbootstrapped_test"
        ] = "unbootstrapped_test",
        provider_input_start_bundle: PreparedProviderInputStartBundle | None,
    ) -> tuple[AgentEvent, ...]:
        normalized_usage = usage_report or TransportUsageReport(
            usage_status="missing", usage=None
        )
        projection = bind_model_terminal_projection_to_session(
            runtime_session,
            projection_reducer.prepare_terminal(
                event_context=event_context,
                terminal_outcome=outcome,
                usage_report=normalized_usage,
                semantic_commit_measurements=semantic_commit_measurements,
                physical_accounting_mode=physical_accounting_mode,
            ),
        )
        retry_delay = 0.01
        while True:
            try:
                await persist_model_terminal_projection(
                    runtime_session,
                    projection,
                    run_id=event_context.run_id,
                )
                break
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None:
                    task.uncancel()
            except (TerminalProjectionPersistenceContractError, ValueError):
                runtime_session.latch_event_commit_outcome_unknown()
                raise
            except Exception as exc:
                from pulsara_agent.memory.foundation.records import (
                    ArtifactContentConflict,
                )

                if isinstance(exc, ArtifactContentConflict):
                    runtime_session.latch_event_commit_outcome_unknown()
                    raise
                if runtime_session.reconciliation_required:
                    raise
            try:
                await asyncio.sleep(retry_delay)
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None:
                    task.uncancel()
            retry_delay = min(1.0, retry_delay * 2)
        runtime_session.transcript_projection_document_registry.register(
            projection.projection_reference,
            projection.document,
        )
        return self._terminal_batch(
            call=call,
            event_context=event_context,
            recovery_plan=recovery_plan,
            validation_estimate=validation_estimate,
            outcome=outcome,
            provider_dispatch_status=provider_dispatch_status,
            usage_report=normalized_usage,
            runtime_session=runtime_session,
            reservation=reservation,
            terminal_projection=projection,
            provider_input_start_bundle=provider_input_start_bundle,
        )

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
        terminal_projection: PreparedModelTerminalProjection,
        provider_input_start_bundle: PreparedProviderInputStartBundle | None,
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
            terminal_projection=terminal_projection.end_reference,
        )
        events: list[AgentEvent] = [
            terminal_projection.committed_event,
            model_end,
        ]
        if (
            provider_input_start_bundle is not None
            and provider_input_start_bundle.is_one_shot
        ):
            from pulsara_agent.runtime.provider_input.planner import (
                build_one_shot_generation_close_event,
            )

            events.append(
                build_one_shot_generation_close_event(
                    bundle=provider_input_start_bundle,
                    event_context=event_context,
                )
            )
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
