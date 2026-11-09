"""Prompt admission and queued prompt or cell-output delivery on the Host session."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from psycopg import InterfaceError, OperationalError

from pulsara_agent.conversation_kernel.annotations import AnnotationSourceInvalid
from pulsara_agent.conversation_kernel.cancellation import (
    ActiveTurnCancellationIntent,
    ForegroundCancellationCause,
)
from pulsara_agent.conversation_kernel.contracts import PromptDeliveryMode
from pulsara_agent.conversation_kernel.current_work_runtime import build_root_activation
from pulsara_agent.conversation_kernel.execution_watchdogs import KernelWatchdogOwner
from pulsara_agent.conversation_kernel.prompt_content import (
    FrozenCanonicalPrompt,
    freeze_canonical_prompt,
)
from pulsara_agent.conversation_kernel.queued_prompt_actions import (
    QueuedPromptAction,
    QueuedPromptActionRejected,
)
from pulsara_agent.conversation_kernel.repository import (
    ConversationKernelConflict,
    PromptIngressRejected,
    StaleHostWriter,
)
from pulsara_agent.conversation_kernel.request_execution import (
    RequestExecution,
    RequestExecutionStatus,
)
from pulsara_agent.conversation_kernel.runner import KernelRunResult
from pulsara_agent.conversation_kernel.steer import (
    PreparedCellOutputIngress,
    PreparedPromptIngressCommand,
    PreparedQueuedRootTurnAdmission,
    PreparedRootTurnIdentity,
    PromptIngressConfirmationKind,
    PromptIngressWriteRejection,
    QueuedRootTurnAdmissionConfirmation,
    QueuedRootTurnAdmissionConfirmationKind,
    build_direct_root_turn_identity,
    build_prompt_ingress_command,
    build_queued_root_turn_identity,
)
from pulsara_agent.hooks.context import PendingHookContextReservation
from pulsara_agent.hooks.contracts import (
    DirectPromptRef,
    GateDecision,
    HookDispatchEnvelope,
    QueuedPromptRef,
    UserPromptSubmitInput,
    external_permission_mode,
)
from pulsara_agent.hooks.matcher import event_matcher_subject
from pulsara_agent.identity import derive_id, is_valid_id, random_id
from pulsara_agent.llm.input import LLMTextPart, PromptContent, prompt_text_projection
from pulsara_agent.llm.model_connections import ModelCallBinding
from pulsara_agent.llm.model_target import FrozenModelResolutionSnapshot
from pulsara_agent.llm.runtime import ModelRuntimeUnavailable
from pulsara_agent.ports.terminal_observation import CellOutputContent
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot
from pulsara_agent.scheduling.contracts import ScheduledAdmission

from .host_contracts import (
    KernelCommandOutcome,
    KernelPromptDelivery,
    PromptBlockedByHook,
    _IngressHookApiVariant,
    _IngressHookReservationKey,
    _PromptAdmissionAttempt,
)


class _HostPromptOperations:
    """Prompt admission and queued prompt or cell-output delivery on the Host session.

    Operations share the concrete Host's existing state and admission locks.
    """

    async def _claim_prompt_admission_attempt(
        self, key: _IngressHookReservationKey | QueuedPromptAction
    ) -> tuple[_PromptAdmissionAttempt, bool]:
        async with self._lock:
            self._require_open()
            current = self._prompt_admission_attempts.get(key.command_id)
            if current is not None:
                if current.key != key:
                    raise ConversationKernelConflict(
                        "in-flight prompt command has a different admission candidate"
                    )
                return current, False
            attempt = _PromptAdmissionAttempt(
                key,
                asyncio.get_running_loop().create_future(),
            )
            self._prompt_admission_attempts[key.command_id] = attempt
            return attempt, True

    async def _settle_prompt_admission_attempt(
        self,
        attempt: _PromptAdmissionAttempt,
        *,
        result: KernelRunResult | KernelCommandOutcome | None = None,
        error: BaseException | None = None,
    ) -> None:
        async with self._lock:
            if self._prompt_admission_attempts.get(attempt.key.command_id) is attempt:
                self._prompt_admission_attempts.pop(attempt.key.command_id, None)
            if attempt.future.done():
                return
            if error is None:
                if result is None:
                    raise RuntimeError("ingress Hook attempt settlement is incomplete")
                attempt.future.set_result(result)
            elif isinstance(error, asyncio.CancelledError):
                attempt.future.cancel()
            else:
                attempt.future.set_exception(error)
                # The owner has already observed the exception.  Mark the shared
                # future as retrieved even when no exact retry joined it.
                attempt.future.exception()

    async def _dispatch_user_prompt_hook(
        self,
        *,
        key: _IngressHookReservationKey,
        identity: PreparedRootTurnIdentity,
        permission: FrozenRunPermissionSnapshot,
        model_call_binding: ModelCallBinding,
        input_origin: str = "HUMAN_MESSAGE",
    ) -> tuple[str | None, PendingHookContextReservation | None]:
        view = self._hooks.capture_view()
        cwd = self._tools.snapshot_workspace_root()
        public_input = UserPromptSubmitInput(
            session_id=self.session_id,
            cwd=str(cwd),
            model=self._model_identity(model_call_binding),
            turn_id=identity.turn_id,
            prompt=prompt_text_projection(key.canonical_prompt.content),
            session_kind="SUBAGENT" if self.root_session_id is not None else "ROOT",
            input_origin=input_origin,
            permission_mode=external_permission_mode(
                permission.effective_mode.value,
                active_plan_workflow=permission.plan_workflow_id is not None,
            ),
        )
        if key.api_variant is _IngressHookApiVariant.DIRECT:
            causal_ref = DirectPromptRef(
                key.command_id,
                identity.turn_id,
                identity.entry_id,
                identity.context_revision_id,
            )
        else:
            assert key.queue_item_id is not None
            causal_ref = QueuedPromptRef(
                key.command_id,
                key.queue_item_id,
                identity.turn_id,
                identity.entry_id,
                identity.context_revision_id,
            )
        outcome = await self._hooks.dispatch(
            HookDispatchEnvelope(
                view,
                self._hook_root_scope,
                public_input,
                causal_ref,
                self._deadlines.deadline(
                    KernelWatchdogOwner.PROVIDER_DISPATCH_PLANNING
                ),
            ),
            matcher_subject=event_matcher_subject(public_input.event_type),
        )
        if outcome.decision is GateDecision.BLOCK:
            return outcome.reason or "UserPromptSubmit Hook blocked", None
        return None, self._hook_context.prepare_sync(
            scope=self._hook_root_scope,
            causal_ref=causal_ref,
            entries=outcome.context_entries,
        )

    async def run_turn(
        self,
        content: PromptContent,
        *,
        command_id: str | None = None,
        requested_permission_mode: PermissionMode | None = None,
    ) -> KernelRunResult:
        self.workspace_gate.require_available()
        frozen_content = await self._image_validator.freeze(
            content,
            deadline_monotonic=self._canonical_deadline(),
        )
        canonical_prompt = freeze_canonical_prompt(
            frozen_content, workspace_id=self.workspace.workspace_key
        )
        command = command_id or random_id("command")
        requested = requested_permission_mode or self._launch_permission_mode
        identity = build_direct_root_turn_identity(self.session_id, command)
        key = _IngressHookReservationKey(
            _IngressHookApiVariant.DIRECT,
            command,
            canonical_prompt,
            requested_permission_mode,
            None,
        )
        attempt, owner = await self._claim_prompt_admission_attempt(key)
        if not owner:
            joined = await asyncio.shield(attempt.future)
            if not isinstance(joined, KernelRunResult):
                raise RuntimeError("direct prompt joined a queued ingress attempt")
            return joined
        try:
            existing = await self._query_command_row(command)
            if existing is not None:
                raise RuntimeError("command was already accepted; query its outcome")
            async with self._lock:
                self._require_open()
                self._retire_done_active_root_locked()
                if (
                    self._compaction.is_fenced(
                        scope_kind=self._session_scope_kind,
                    )
                    or self._plan_exit_fence
                    or self._external_new_turn_accepting
                    or self._active_task is not None
                    or self._manual_compaction_command_attempts
                ):
                    raise RuntimeError("a canonical ROOT turn is already running")
            permission = await self._io.run(
                self.repository.prepare_root_permission_snapshot,
                self._lease.guard,
                snapshot_id=identity.permission_snapshot_id,
                requested_mode=requested,
                deadline_monotonic=self._canonical_deadline(),
            )
            model_resolution_snapshot = self._model_runtime.freeze_resolution_snapshot()
            model_call_binding = await self.model_call_binding()
            if model_call_binding is None:
                raise ValueError("session has no model configuration")
            model_call_binding, _resolved, _changed = (
                model_resolution_snapshot.reconcile(model_call_binding)
            )
            block_reason, context_reservation = await self._dispatch_user_prompt_hook(
                key=key,
                identity=identity,
                permission=permission,
                model_call_binding=model_call_binding,
                input_origin="HUMAN_MESSAGE",
            )
            if block_reason is not None:
                raise PromptBlockedByHook(block_reason)
            async with self._lock:
                self._require_open()
                self._retire_done_active_root_locked()
                if (
                    self._prompt_admission_attempts.get(command) is not attempt
                    or self._compaction.is_fenced(
                        scope_kind=self._session_scope_kind,
                    )
                    or self._plan_exit_fence
                    or self._external_new_turn_accepting
                    or self._active_task is not None
                    or self._manual_compaction_command_attempts
                ):
                    if context_reservation is not None:
                        context_reservation.retire()
                    raise RuntimeError("prompt Hook admission became stale")
                task = self._install_active_root_task_locked(
                    turn_id=identity.turn_id,
                    started=False,
                    command_id=command,
                    name=f"kernel-turn:{command}",
                    run=lambda intent: self._run_root_turn_chain(
                        canonical_prompt,
                        command_id=command,
                        requested_permission_mode=requested,
                        expected_permission_snapshot=permission,
                        hook_context_reservation=context_reservation,
                        cancellation_intent=intent,
                        model_resolution_snapshot=model_resolution_snapshot,
                    ),
                )
            result = await asyncio.shield(task)
        except BaseException as exc:
            await self._settle_prompt_admission_attempt(attempt, error=exc)
            raise
        await self._settle_prompt_admission_attempt(attempt, result=result)
        return result

    async def submit_prompt(
        self,
        *,
        command_id: str,
        content: PromptContent,
        delivery_mode: PromptDeliveryMode = PromptDeliveryMode.NEW_TURN,
        target_turn_id: str | None = None,
        requested_permission_mode: PermissionMode | None = None,
        _scheduled: ScheduledAdmission | None = None,
    ) -> KernelCommandOutcome:
        if not is_valid_id(command_id, "command"):
            return KernelCommandOutcome(
                command_id, "REJECTED", "", "INVALID_ID", "Command is invalid."
            )
        if (delivery_mode is PromptDeliveryMode.NEW_TURN) != (target_turn_id is None):
            return KernelCommandOutcome(
                command_id,
                "REJECTED",
                "",
                "INVALID_STEER_TARGET",
                "Prompt delivery target is invalid.",
            )
        try:
            frozen_content = await self._image_validator.freeze(
                content,
                deadline_monotonic=self._canonical_deadline(),
            )
            canonical_prompt = freeze_canonical_prompt(
                frozen_content, workspace_id=self.workspace.workspace_key
            )
        except ValueError as exc:
            return KernelCommandOutcome(
                command_id, "REJECTED", "", "INVALID_PROMPT", str(exc)
            )
        if _scheduled is not None and (
            delivery_mode is not PromptDeliveryMode.NEW_TURN
            or _scheduled.session_id != self.session_id
            or canonical_prompt.content.parts != (LLMTextPart(_scheduled.prompt),)
            or requested_permission_mode != _scheduled.permission_mode
            or (_scheduled.manual and _scheduled.client_command_id != command_id)
        ):
            return KernelCommandOutcome(
                command_id, "REJECTED", "", "INVALID_REQUEST", "定时输入身份无效。"
            )
        self.workspace_gate.require_available()
        queue_item_id = derive_id("queue", "ingress", self.session_id, command_id)
        key = _IngressHookReservationKey(
            _IngressHookApiVariant.QUEUED,
            command_id,
            canonical_prompt,
            requested_permission_mode,
            queue_item_id,
            _scheduled,
            delivery_mode,
            target_turn_id,
        )
        try:
            attempt, owner = await self._claim_prompt_admission_attempt(key)
        except ConversationKernelConflict:
            return KernelCommandOutcome(
                command_id,
                "REJECTED",
                queue_item_id,
                "COMMAND_CONFLICT",
                "The command identity names a different in-flight prompt.",
            )
        if not owner:
            joined = await asyncio.shield(attempt.future)
            if not isinstance(joined, KernelCommandOutcome):
                raise RuntimeError("queued prompt joined a direct ingress attempt")
            return joined

        async def publish() -> KernelCommandOutcome:
            try:
                result = await self._submit_prompt_owner(
                    command_id=command_id,
                    canonical_prompt=canonical_prompt,
                    delivery_mode=delivery_mode,
                    target_turn_id=target_turn_id,
                    requested_permission_mode=requested_permission_mode,
                    _scheduled=_scheduled,
                    hook_attempt=attempt,
                )
            except BaseException as exc:
                await self._settle_prompt_admission_attempt(attempt, error=exc)
                raise
            await self._settle_prompt_admission_attempt(attempt, result=result)
            return result

        attempt.task = asyncio.create_task(
            publish(), name=f"kernel-prompt-admission:{command_id}"
        )
        attempt.task.add_done_callback(
            lambda task: task.exception() if not task.cancelled() else None
        )
        return await asyncio.shield(attempt.task)

    async def _submit_prompt_owner(
        self,
        *,
        command_id: str,
        canonical_prompt: FrozenCanonicalPrompt,
        delivery_mode: PromptDeliveryMode = PromptDeliveryMode.NEW_TURN,
        target_turn_id: str | None = None,
        requested_permission_mode: PermissionMode | None = None,
        _scheduled: ScheduledAdmission | None = None,
        hook_attempt: _PromptAdmissionAttempt | None = None,
    ) -> KernelCommandOutcome:
        if not isinstance(canonical_prompt, FrozenCanonicalPrompt):
            raise TypeError("prompt owner requires a frozen canonical prompt")
        self._require_open()
        queue_item_id = derive_id("queue", "ingress", self.session_id, command_id)
        permission_snapshot_id = (
            None
            if delivery_mode is PromptDeliveryMode.STEER_ACTIVE_TURN
            else derive_id(
                "permission",
                "admission",
                build_queued_root_turn_identity(self.session_id, queue_item_id).turn_id,
            )
        )
        effective_requested_permission = (
            None
            if delivery_mode is PromptDeliveryMode.STEER_ACTIVE_TURN
            else requested_permission_mode or self._launch_permission_mode
        )
        ingress = build_prompt_ingress_command(
            session_id=self.session_id,
            command_id=command_id,
            queue_item_id=queue_item_id,
            client_submission_id=command_id,
            delivery_mode=delivery_mode,
            target_turn_id=target_turn_id,
            permission_snapshot_id=permission_snapshot_id,
            requested_permission_mode=effective_requested_permission,
            canonical_prompt=canonical_prompt,
            scheduled=_scheduled,
        )
        confirmation = await self._io.run(
            self.repository.confirm_prompt_ingress,
            candidate=ingress,
            deadline_monotonic=self._canonical_deadline(),
        )
        if confirmation.kind is PromptIngressConfirmationKind.CONFLICT:
            return KernelCommandOutcome(
                command_id,
                "REJECTED",
                queue_item_id,
                "COMMAND_CONFLICT",
                "The command identity names a different prompt.",
            )
        if confirmation.kind is PromptIngressConfirmationKind.FULL_COMPATIBLE:
            # The row is the level-triggered delivery truth.  A prior caller
            # may have lost the COMMIT acknowledgement before it could wake
            # the process-local queue loop, so every compatible retry must
            # re-assert the wake hint.
            self._queue_wake.set()
            existing = await self.query_command(command_id)
            if existing is None:
                raise RuntimeError("compatible prompt command has no canonical outcome")
            if (
                existing.prompt_delivery is not None
                and existing.prompt_delivery.delivery_mode == "STEER_ACTIVE_TURN"
            ):
                await self._notify_accepted_supplement(existing)
            return existing
        model_resolution_snapshot: FrozenModelResolutionSnapshot | None = None
        model_call_binding: ModelCallBinding | None = None
        if _scheduled is not None:
            try:
                model_resolution_snapshot = (
                    self._model_runtime.freeze_resolution_snapshot()
                )
            except ModelRuntimeUnavailable:
                # The writer may select an active turn, whose existing request
                # and permission do not depend on this future-turn catalog.
                model_resolution_snapshot = None
        if delivery_mode is PromptDeliveryMode.NEW_TURN and _scheduled is None:
            model_call_binding = await self.model_call_binding()
            if model_call_binding is None:
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    queue_item_id,
                    "MODEL_CONFIGURATION_REQUIRED",
                    "Select a model configuration before sending.",
                )
            try:
                model_resolution_snapshot = (
                    self._model_runtime.freeze_resolution_snapshot()
                )
                model_call_binding, _resolved, _changed = (
                    model_resolution_snapshot.reconcile(model_call_binding)
                )
            except (KeyError, ValueError, ModelRuntimeUnavailable) as exc:
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    queue_item_id,
                    "MODEL_CONFIGURATION_UNAVAILABLE",
                    str(exc),
                )
        permission_projection: FrozenRunPermissionSnapshot | None = None
        hook_context_reservation: PendingHookContextReservation | None = None
        if delivery_mode is PromptDeliveryMode.NEW_TURN and _scheduled is None:
            if hook_attempt is None:
                raise RuntimeError("queued new turn lacks its Hook reservation owner")
            identity = build_queued_root_turn_identity(self.session_id, queue_item_id)
            if permission_snapshot_id != identity.permission_snapshot_id:
                raise RuntimeError("queued prompt permission identity drifted")
            assert effective_requested_permission is not None
            permission_projection = await self._io.run(
                self.repository.prepare_root_permission_snapshot,
                self._lease.guard,
                snapshot_id=identity.permission_snapshot_id,
                requested_mode=effective_requested_permission,
                deadline_monotonic=self._canonical_deadline(),
                queued_user_observation=_scheduled is None,
            )
            if not isinstance(hook_attempt.key, _IngressHookReservationKey):
                raise RuntimeError("prompt Hook requires an ingress candidate")
            (
                block_reason,
                hook_context_reservation,
            ) = await self._dispatch_user_prompt_hook(
                key=hook_attempt.key,
                identity=identity,
                permission=permission_projection,
                model_call_binding=model_call_binding,
                input_origin="SCHEDULED_TASK"
                if _scheduled is not None
                else "HUMAN_MESSAGE",
            )
            if block_reason is not None:
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    queue_item_id,
                    "HOOK_BLOCKED",
                    block_reason,
                )
        async with self._lock:
            if (
                self._closing
                or self._plan_exit_fence
                or (
                    hook_attempt is not None
                    and self._prompt_admission_attempts.get(command_id)
                    is not hook_attempt
                )
            ):
                if hook_context_reservation is not None:
                    hook_context_reservation.retire()
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    "",
                    "PLAN_TRANSITION_BUSY",
                    "A Plan force-exit transition is in progress.",
                )
        try:
            return await self._publish_prompt(
                command_id=command_id,
                queue_item_id=queue_item_id,
                delivery_mode=delivery_mode,
                target_turn_id=target_turn_id,
                permission_snapshot_id=permission_snapshot_id,
                effective_requested_permission=effective_requested_permission,
                ingress=ingress,
                model_resolution_snapshot=model_resolution_snapshot,
                expected_permission_snapshot=permission_projection,
                hook_context_reservation=hook_context_reservation,
            )
        except BaseException:
            if hook_context_reservation is not None:
                hook_context_reservation.retire()
            raise

    async def _publish_prompt(
        self,
        *,
        command_id: str,
        queue_item_id: str,
        delivery_mode: PromptDeliveryMode,
        target_turn_id: str | None,
        permission_snapshot_id: str | None,
        effective_requested_permission: PermissionMode | None,
        ingress: PreparedPromptIngressCommand,
        model_resolution_snapshot: FrozenModelResolutionSnapshot | None,
        expected_permission_snapshot: FrozenRunPermissionSnapshot | None,
        hook_context_reservation: PendingHookContextReservation | None,
    ) -> KernelCommandOutcome:
        try:
            self.workspace_gate.require_available()
            accepted = await self._io.run(
                self.repository.enqueue_prompt,
                self._lease.guard,
                candidate=ingress,
                model_resolution_snapshot=model_resolution_snapshot,
                occurred_at=datetime.now().astimezone(),
                actor_id=self.host_session_id,
                deadline_monotonic=self._canonical_deadline(),
                _expected_permission_snapshot=expected_permission_snapshot,
            )
        except AnnotationSourceInvalid as exc:
            if hook_context_reservation is not None:
                hook_context_reservation.retire()
            return KernelCommandOutcome(
                command_id, "REJECTED", queue_item_id, "INVALID_ANNOTATION", str(exc)
            )
        except PromptIngressRejected as exc:
            if hook_context_reservation is not None:
                hook_context_reservation.retire()
            if exc.reason is PromptIngressWriteRejection.COMMAND_CONFLICT:
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    queue_item_id,
                    "COMMAND_CONFLICT",
                    "The command identity names a different prompt.",
                )
            if exc.reason is PromptIngressWriteRejection.CAPACITY_EXHAUSTED:
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    queue_item_id,
                    "PROMPT_CAPACITY_EXHAUSTED",
                    "The session prompt queue is full.",
                )
            if exc.reason is (PromptIngressWriteRejection.INGRESS_PRECONDITION_CHANGED):
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    queue_item_id,
                    "INGRESS_PRECONDITION_CHANGED",
                    "The Plan or permission cut changed while the Hook ran.",
                )
            if exc.reason is PromptIngressWriteRejection.MODEL_CONFIGURATION_REQUIRED:
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    queue_item_id,
                    "MODEL_CONFIGURATION_REQUIRED",
                    "Select a model configuration before sending.",
                )
            if (
                exc.reason
                is PromptIngressWriteRejection.MODEL_CONFIGURATION_UNAVAILABLE
            ):
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    queue_item_id,
                    "MODEL_CONFIGURATION_UNAVAILABLE",
                    "The selected model configuration is no longer executable.",
                )
            if exc.reason is (
                PromptIngressWriteRejection.TARGET_STALE_OR_NON_STEERABLE
            ):
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    target_turn_id or "",
                    "STEER_TARGET_STALE",
                    "The target turn no longer accepts steering.",
                )
            raise
        except Exception:
            confirmation = await self._io.run(
                self.repository.confirm_prompt_ingress,
                candidate=ingress,
                deadline_monotonic=self._canonical_deadline(),
            )
            if confirmation.kind is PromptIngressConfirmationKind.FULL_COMPATIBLE:
                existing = await self.query_command(command_id)
                if existing is None:
                    raise RuntimeError(
                        "compatible prompt command has no canonical outcome"
                    )
                if hook_context_reservation is not None:
                    if existing.target_id == queue_item_id:
                        hook_context_reservation.commit_prompt_bound(queue_item_id)
                    else:
                        hook_context_reservation.retire()
                self._queue_wake.set()
                existing = await self.query_command(command_id)
                if existing is None:
                    raise RuntimeError(
                        "compatible prompt command has no canonical outcome"
                    )
                if (
                    existing.prompt_delivery is not None
                    and existing.prompt_delivery.delivery_mode == "STEER_ACTIVE_TURN"
                ):
                    await self._notify_accepted_supplement(existing)
                return existing
            if confirmation.kind is PromptIngressConfirmationKind.CONFLICT:
                if hook_context_reservation is not None:
                    hook_context_reservation.retire()
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    queue_item_id,
                    "COMMAND_CONFLICT",
                    "The command identity names a different prompt.",
                )
            raise
        delivery_mode = accepted.delivery_mode or delivery_mode
        target_turn_id = accepted.target_turn_id
        if (
            accepted.queue_item_id is not None
            and accepted.queue_item_id != queue_item_id
        ):
            queue_item_id = accepted.queue_item_id
            if hook_context_reservation is not None:
                hook_context_reservation.retire()
                hook_context_reservation = None
        if hook_context_reservation is not None:
            hook_context_reservation.commit_prompt_bound(queue_item_id)
        self._queue_wake.set()
        outcome = KernelCommandOutcome(
            command_id,
            "PENDING",
            queue_item_id,
            (
                "PROMPT_QUEUED_REASONING_UPDATED"
                if accepted.reasoning_preference_reset
                else "PROMPT_QUEUED"
            ),
            (
                "Reasoning options changed; the new target default was selected."
                if accepted.reasoning_preference_reset
                else f"Prompt accepted at queue sequence {accepted.queue_sequence}."
            ),
            prompt_delivery=KernelPromptDelivery(
                queue_item_id=queue_item_id,
                queue_status="PENDING",
                consumed_entry_id=None,
                delivery_mode=delivery_mode.value,
                execution=(
                    RequestExecution(RequestExecutionStatus.QUEUED)
                    if delivery_mode is PromptDeliveryMode.NEW_TURN
                    else None
                ),
            ),
        )

        self._ephemeral.invalidate(preserve_running=True)

        if delivery_mode is PromptDeliveryMode.STEER_ACTIVE_TURN:
            await self._notify_accepted_supplement(outcome)
        return outcome

    async def _notify_accepted_supplement(self, outcome: KernelCommandOutcome) -> None:
        delivery = outcome.prompt_delivery
        if delivery is None or delivery.queue_status != "PENDING":
            return
        await self._agent_runtime.notify_changed()

    async def cancel_queued_prompt(
        self, *, command_id: str, source_queue_item_id: str
    ) -> KernelCommandOutcome:
        return await self._apply_queued_prompt_action(
            QueuedPromptAction(
                self.session_id,
                command_id,
                source_queue_item_id,
            )
        )

    async def steer_queued_prompt(
        self, *, command_id: str, source_queue_item_id: str, target_turn_id: str
    ) -> KernelCommandOutcome:
        return await self._apply_queued_prompt_action(
            QueuedPromptAction(
                self.session_id,
                command_id,
                source_queue_item_id,
                target_turn_id,
            )
        )

    async def _apply_queued_prompt_action(
        self, candidate: QueuedPromptAction
    ) -> KernelCommandOutcome:
        try:
            attempt, owner = await self._claim_prompt_admission_attempt(candidate)
        except ConversationKernelConflict:
            return KernelCommandOutcome(
                candidate.command_id,
                "REJECTED",
                "",
                "COMMAND_CONFLICT",
                "该操作身份已经用于另一项操作。",
            )
        if not owner:
            outcome = await asyncio.shield(attempt.future)
            if not isinstance(outcome, KernelCommandOutcome):
                raise ConversationKernelConflict("queue action joined a direct prompt")
            return outcome

        async def publish() -> KernelCommandOutcome:
            try:
                outcome = await self._apply_queued_prompt_action_owned(candidate)
            except BaseException as error:
                await self._settle_prompt_admission_attempt(attempt, error=error)
                raise
            await self._settle_prompt_admission_attempt(attempt, result=outcome)
            return outcome

        attempt.task = asyncio.create_task(
            publish(), name=f"kernel-queue-action:{candidate.command_id}"
        )
        attempt.task.add_done_callback(
            lambda task: task.exception() if not task.cancelled() else None
        )
        return await asyncio.shield(attempt.task)

    async def _apply_queued_prompt_action_owned(
        self, candidate: QueuedPromptAction
    ) -> KernelCommandOutcome:
        # Reserve only process-local admission here; no database IO under the
        # Host lock. The existing canonical writer resolves target/source/fence.
        async with self._lock:
            self._require_open()
            if self._plan_exit_fence:
                return KernelCommandOutcome(
                    candidate.command_id,
                    "REJECTED",
                    "",
                    "PLAN_TRANSITION_BUSY",
                    "A Plan force-exit transition is in progress.",
                )
        try:
            await self._io.run(
                self.repository.apply_queued_prompt_action,
                self._lease.guard,
                candidate=candidate,
                occurred_at=datetime.now().astimezone(),
                actor_id=self.host_session_id,
                deadline_monotonic=self._canonical_deadline(),
            )
        except QueuedPromptActionRejected as error:
            return KernelCommandOutcome(
                candidate.command_id,
                "REJECTED",
                "",
                error.code,
                {
                    "COMMAND_CONFLICT": "该操作身份已经用于另一项操作。",
                    "PROMPT_NOT_PENDING": "这条输入已不在等待队列中。",
                    "SCHEDULED_INPUT_CANNOT_STEER": "定时输入只能按队列顺序处理。",
                    "SOURCE_CANNOT_STEER": "这类输入不能手动改为当前任务引导。",
                    "PROMPT_ALREADY_CONSUMED": "这条输入已开始处理。",
                    "STEER_TARGET_CLOSED": "当前任务已结束；输入仍按队列顺序处理。",
                }[error.code],
            )
        except Exception:
            # Confirm the original action only. Never reissue a mutation
            # after an unknown ACK, or infer a winner from source text.
            row = await self._query_command_row(candidate.command_id)
            if row is None:
                raise
            if (
                row["command_kind"] != candidate.command_kind
                or row["semantic_digest"] != candidate.semantic_digest
                or row["target_queue_item_id"] != candidate.target_queue_item_id
            ):
                raise ConversationKernelConflict("queue action confirmation conflicts")
        self._hook_context.retire_prompt_candidate(
            scope=self._hook_root_scope,
            prompt_candidate_id=candidate.source_queue_item_id,
        )
        self._queue_wake.set()
        outcome = await self.query_command(candidate.command_id)
        if outcome is None:
            raise ConversationKernelConflict("queue action has no canonical winner")
        if candidate.target_turn_id is not None:
            await self._notify_accepted_supplement(outcome)
        return outcome

    async def steer_active_turn(
        self, *, command_id: str, content: PromptContent, target_turn_id: str
    ) -> KernelCommandOutcome:
        # The stable command is queried before any process-local liveness
        # shortcut.  This lets a retry recover a committed queue winner after
        # its ACK was lost even if the target turn has since become terminal.
        # A genuinely new write is still exact-target validated by the
        # repository in the Host-writer transaction.
        return await self.submit_prompt(
            command_id=command_id,
            content=content,
            delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
            target_turn_id=target_turn_id,
        )

    async def _prompt_delivery_loop(self) -> None:
        while True:
            await self._queue_wake.wait()
            self._queue_wake.clear()
            if self._closing:
                return
            while not self._closing:
                await self.workspace_gate.wait_available()
                async with self._lock:
                    self._retire_done_active_root_locked()
                    active = self._active_task
                    external_new_turn_accepting = self._external_new_turn_accepting
                    plan_exit_fence = self._plan_exit_fence
                    reopen_gated = self._runtime_reopen_quiescence is not None
                    compaction_fenced = self._compaction.is_fenced(
                        scope_kind=self._session_scope_kind,
                    )
                if reopen_gated:
                    break
                if compaction_fenced or self._manual_compaction_command_attempts:
                    await asyncio.sleep(0.05)
                    self._queue_wake.set()
                    break
                if plan_exit_fence:
                    await asyncio.sleep(0)
                    continue
                if external_new_turn_accepting:
                    await asyncio.sleep(0)
                    continue
                if active is not None:
                    try:
                        await asyncio.shield(active)
                    except BaseException:
                        pass
                    continue
                try:
                    candidate = await self._io.run(
                        self.repository.prepare_prompt_head_consumption,
                        session_id=self.session_id,
                        occurred_at=datetime.now().astimezone(),
                        actor_id=self.host_session_id,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await asyncio.sleep(0.1)
                    self._queue_wake.set()
                    break
                if candidate is None:
                    if self.root_session_id is not None:
                        await self._agent_runtime.release(self.session_id)
                    break
                if (
                    self.root_session_id is not None
                    and not await self._agent_runtime.try_acquire(self.session_id)
                ):
                    break
                try:
                    model_resolution_snapshot = (
                        self._model_runtime.freeze_resolution_snapshot()
                    )
                except ModelRuntimeUnavailable:
                    if self.root_session_id is not None:
                        await self._agent_runtime.release(self.session_id)
                    # No trustworthy process-local catalog/settings cut exists.
                    # The canonical queue row stays pending until an ordinary
                    # refresh or settings repair wakes delivery again.
                    await asyncio.sleep(0.1)
                    self._queue_wake.set()
                    break
                try:
                    model_resolution_snapshot.validate(candidate.model_call_binding)
                except (KeyError, ValueError):
                    if self.root_session_id is not None:
                        await self._agent_runtime.release(self.session_id)
                    rejected = await self._settle_queued_model_rejection(candidate)
                    if rejected:
                        self._queue_wake.set()
                        continue
                    break
                task = None
                try:
                    if not await self._prepare_queued_prompt_hook(candidate):
                        self._queue_wake.set()
                        continue
                    async with self._lock:
                        self._retire_done_active_root_locked()
                        if (
                            self._closing
                            or self._active_task is not None
                            or self._external_new_turn_accepting
                            or self._plan_exit_fence
                            or self._compaction.has_active_work()
                        ):
                            self._queue_wake.set()
                            break
                        task = self._install_active_root_task_locked(
                            turn_id=candidate.exact_turn_id,
                            started=False,
                            command_id=None,
                            name=f"kernel-queued-turn:{candidate.exact_turn_id}",
                            work_id=candidate.queue_item_id
                            if self.root_session_id is not None
                            else None,
                            run=lambda intent: self._run_queued_root_admission(
                                candidate, intent
                            ),
                        )
                    # Closing or cancelling this scheduler never cancels the
                    # task that owns start confirmation and physical capacity.
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        if self._closing:
                            raise
                    except Exception:
                        pass
                finally:
                    if task is None and self.root_session_id is not None:
                        await self._agent_runtime.release(self.session_id)

    async def _settle_queued_model_rejection(
        self,
        candidate: PreparedQueuedRootTurnAdmission,
    ) -> bool:
        """Settle one permanent pre-delivery failure without detaching its ACK."""

        delay_seconds = 0.05
        while not self._closing:
            try:
                return await self._io.run(
                    self.repository.reject_prepared_prompt_head_model_unavailable,
                    self._lease.guard,
                    candidate=candidate,
                    deadline_monotonic=self._canonical_deadline(),
                )
            except asyncio.CancelledError:
                raise
            except StaleHostWriter:
                return False
            except Exception:
                await asyncio.sleep(delay_seconds)
                delay_seconds = min(delay_seconds * 2, 0.5)
        return False

    async def _prepare_queued_prompt_hook(
        self,
        candidate: PreparedQueuedRootTurnAdmission,
    ) -> bool:
        scheduled = candidate.scheduled_input is not None
        if self.root_session_id is None and not scheduled:
            return True
        outcome = self._queued_prompt_hooks.get(candidate.queue_item_id)
        if outcome is None:
            key = _IngressHookReservationKey(
                _IngressHookApiVariant.QUEUED,
                candidate.command_id,
                candidate.canonical_prompt,
                candidate.permission_snapshot.requested_mode,
                candidate.queue_item_id,
                None,
                PromptDeliveryMode.NEW_TURN,
                None,
            )
            outcome = await self._dispatch_user_prompt_hook(
                key=key,
                identity=build_queued_root_turn_identity(
                    self.session_id, candidate.queue_item_id
                ),
                permission=candidate.permission_snapshot,
                model_call_binding=candidate.model_call_binding,
                input_origin="SCHEDULED_TASK" if scheduled else "AGENT_INSTRUCTION",
            )
            self._queued_prompt_hooks[candidate.queue_item_id] = outcome
            if outcome[1] is not None:
                outcome[1].commit_prompt_bound(candidate.queue_item_id)
        if outcome[0] is None:
            return True
        await self._io.run(
            self.repository.reject_prepared_prompt_head_hook_blocked,
            self._lease.guard,
            candidate=candidate,
            deadline_monotonic=self._canonical_deadline(),
        )
        self._queued_prompt_hooks.pop(candidate.queue_item_id, None)
        await self._agent_runtime.notify_changed()
        return False

    @staticmethod
    async def _drain_start_settlement(task: asyncio.Task):
        """Keep the exact mutation result attached through repeated stop/close."""
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                break
        return task.result(), cancelled

    async def _settle_queued_start(
        self,
        candidate: PreparedQueuedRootTurnAdmission,
        intent: ActiveTurnCancellationIntent,
    ) -> QueuedRootTurnAdmissionConfirmation | None:
        delay = 0.05
        while not self._closing and intent.cause is None:
            await self.workspace_gate.wait_available()
            try:
                return await self._io.run(
                    self.repository.consume_prepared_prompt_head,
                    self._lease.guard,
                    candidate=candidate,
                    deadline_monotonic=self._canonical_deadline(),
                )
            except StaleHostWriter:
                return None
            except Exception as error:
                # An unknown acknowledgement confirms this original candidate;
                # an authoritative NONE permits only a transient I/O retry.
                while True:
                    try:
                        await self._io.run(
                            self.repository.validate_host_writer,
                            self._lease.guard,
                            deadline_monotonic=self._canonical_deadline(),
                        )
                        confirmation = await self._io.run(
                            self.repository.confirm_prepared_prompt_head_consumption,
                            candidate=candidate,
                            deadline_monotonic=self._canonical_deadline(),
                        )
                        break
                    except StaleHostWriter:
                        return None
                    except (TimeoutError, OSError, OperationalError, InterfaceError):
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 0.5)
                if (
                    confirmation.kind
                    is not QueuedRootTurnAdmissionConfirmationKind.NONE
                ):
                    return confirmation
                if self._closing or intent.cause is not None:
                    return None
                if not isinstance(
                    error, (TimeoutError, OSError, OperationalError, InterfaceError)
                ):
                    # This logical request was definitively refused before
                    # start. Leaving it pending would make the scheduler retry
                    # the same business rejection as a new physical attempt.
                    while True:
                        try:
                            await self._io.run(
                                self.repository.reject_prepared_prompt_head_start_rejected,
                                self._lease.guard,
                                candidate=candidate,
                                deadline_monotonic=self._canonical_deadline(),
                            )
                            break
                        except StaleHostWriter:
                            return None
                        except (
                            TimeoutError,
                            OSError,
                            OperationalError,
                            InterfaceError,
                        ):
                            await asyncio.sleep(delay)
                            delay = min(delay * 2, 0.5)
                    raise error
                await asyncio.sleep(delay)
                delay = min(delay * 2, 0.5)
        return None

    async def _run_queued_root_admission(
        self,
        candidate: PreparedQueuedRootTurnAdmission,
        intent: ActiveTurnCancellationIntent,
    ) -> KernelRunResult | None:
        """The sole task owns start, preparation, execution and terminal cleanup."""
        accepted = None
        execution_entered = False
        self._hook_context.activate_prompt_candidate(
            scope=self._hook_root_scope,
            prompt_candidate_id=candidate.queue_item_id,
        )
        try:
            confirmation, cancelled = await self._drain_start_settlement(
                asyncio.create_task(
                    self._settle_queued_start(candidate, intent),
                    name=f"kernel-queued-start:{candidate.queue_item_id}",
                )
            )
            if (
                confirmation is None
                or confirmation.kind is not QueuedRootTurnAdmissionConfirmationKind.FULL
            ):
                return None
            accepted = confirmation.accepted
            assert accepted is not None
            self._queued_prompt_hooks.pop(candidate.queue_item_id, None)
            if self._scheduled_tasks is not None:
                self._scheduled_tasks.wake()
            status = await self._io.run(
                self.repository.read_turn_status,
                session_id=self.session_id,
                turn_id=accepted.turn_id,
                deadline_monotonic=self._canonical_deadline(),
            )
            if status is None or status.value != "RUNNING":
                return None
            if cancelled or self._closing or intent.cause is not None:
                raise asyncio.CancelledError
            await self._io.run(
                self.repository.validate_host_writer,
                self._lease.guard,
                deadline_monotonic=self._canonical_deadline(),
            )
            async with self._lock:
                if self._closing or intent.cause is not None:
                    raise asyncio.CancelledError
                if self._active_turn_id == accepted.turn_id:
                    self._active_turn_started = True
            activation = build_root_activation(
                session_id=candidate.session_id,
                admission_kind="QUEUED",
                queue_item_id=candidate.queue_item_id,
                queue_sequence=candidate.queue_sequence,
                exact_turn_id=candidate.exact_turn_id,
                exact_initial_entry_id=candidate.exact_initial_entry_id,
                exact_context_binding_revision_id=candidate.exact_context_binding_revision_id,
            )
            _closed, finalizer_cancelled = await self._drain_start_settlement(
                asyncio.create_task(
                    self._finalize_root_work_activation(activation, accepted),
                    name=f"kernel-queued-todo-finalizer:{accepted.turn_id}",
                )
            )
            if finalizer_cancelled or self._closing or intent.cause is not None:
                raise asyncio.CancelledError
            execution_entered = True
            return await self._run_accepted_root_chain(
                accepted.turn_id,
                intent,
                started_input_group=accepted.started_input_group,
            )
        except StaleHostWriter:
            # The takeover transaction owns canonical interruption.
            return None
        except Exception:
            if accepted is None:
                # A deterministic confirmer failure must not strand a pending
                # request either. The existing rejection transaction refuses
                # to overwrite a canonical started winner.
                try:
                    await self._drain_start_settlement(
                        asyncio.create_task(
                            self._reject_failed_queued_start(candidate),
                            name=f"kernel-queued-start-rejection:{candidate.queue_item_id}",
                        )
                    )
                except ConversationKernelConflict:
                    pass
            raise
        finally:
            if accepted is not None and not execution_entered:
                reason = (
                    "USER_STOPPED"
                    if intent.cause is ForegroundCancellationCause.USER_REQUEST
                    else "SESSION_CLOSED"
                    if self._closing
                    or intent.cause is ForegroundCancellationCause.HOST_SESSION_CLOSE
                    else "FOREGROUND_EXECUTION_INTERRUPTED"
                )
                await self._drain_start_settlement(
                    asyncio.create_task(
                        self._interrupt_queued_admission_until_safe(
                            turn_id=accepted.turn_id, reason=reason
                        ),
                        name=f"kernel-queued-start-terminal:{accepted.turn_id}",
                    )
                )
            if not execution_entered:
                self._hook_context.retire_prompt_candidate(
                    scope=self._hook_root_scope,
                    prompt_candidate_id=candidate.queue_item_id,
                )

    async def _reject_failed_queued_start(self, candidate) -> None:
        while True:
            try:
                await self._io.run(
                    self.repository.reject_prepared_prompt_head_start_rejected,
                    self._lease.guard,
                    candidate=candidate,
                    deadline_monotonic=self._canonical_deadline(),
                )
                return
            except StaleHostWriter:
                return
            except (TimeoutError, OSError, OperationalError, InterfaceError):
                await asyncio.sleep(0.05)

    async def _interrupt_queued_admission_until_safe(
        self, *, turn_id: str, reason: str
    ) -> None:
        delay_seconds = 0.05
        while True:
            try:
                await self._io.run(
                    self.repository.interrupt_turn,
                    self._lease.guard,
                    turn_id=turn_id,
                    reason=reason,
                    occurred_at=datetime.now().astimezone(),
                    actor_id=self.host_session_id,
                    deadline_monotonic=self._canonical_deadline(),
                )
            except StaleHostWriter:
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            try:
                status = await self._io.run(
                    self.repository.read_turn_status,
                    session_id=self.session_id,
                    turn_id=turn_id,
                    deadline_monotonic=self._canonical_deadline(),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(delay_seconds)
                delay_seconds = min(delay_seconds * 2, 0.5)
                continue
            if status is None or status.value != "RUNNING":
                return
            await asyncio.sleep(delay_seconds)
            delay_seconds = min(delay_seconds * 2, 0.5)

    async def _cell_output_delivery_loop(self) -> None:
        """Transfer each immutable Cell record to the canonical input queue."""
        while True:
            await self._cell_output_wake.wait()
            self._cell_output_wake.clear()
            redrive = False
            for cell_id in self._code_mode.pending_output_cells():
                lease = None
                candidate = None
                try:
                    lease = await self._code_mode.lease_output(cell_id)
                    if lease is None:
                        continue
                    candidate = PreparedCellOutputIngress(
                        session_id=self.session_id,
                        workspace_id=self.workspace.workspace_key,
                        content=CellOutputContent(
                            cell_id=lease.cell_id,
                            source_turn_id=lease.source_turn_id,
                            source_exec_attempt_id=lease.source_exec_attempt_id,
                            record_sequence=lease.record_sequence,
                            record=lease.record,
                        ),
                        occurred_at=lease.occurred_at,
                        actor_id=self.host_session_id,
                    )
                    confirmation = await self._io.run(
                        self.repository.confirm_cell_output_ingress,
                        candidate=candidate,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                    if confirmation.kind is PromptIngressConfirmationKind.CONFLICT:
                        raise ConversationKernelConflict(
                            "Cell output identity conflicts"
                        )
                    if (
                        confirmation.kind
                        is PromptIngressConfirmationKind.FULL_COMPATIBLE
                    ):
                        await self._code_mode.acknowledge_output(lease)
                        self._queue_wake.set()
                        continue
                    if self._closing or not self._code_mode.output_delivery_allowed(
                        lease
                    ):
                        await self._code_mode.acknowledge_output(lease, accepted=False)
                        continue
                    await self._io.run(
                        self.repository.enqueue_cell_output,
                        self._lease.guard,
                        candidate=candidate,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                    await self._code_mode.acknowledge_output(lease)
                    self._queue_wake.set()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    try:
                        settled = await self._settle_cell_output_failure(
                            lease, candidate, error
                        )
                    except Exception:
                        settled = False
                        logging.getLogger(
                            "pulsara_agent.conversation_kernel.host"
                        ).exception("Cell output acceptance remains unconfirmed")
                    redrive = redrive or not settled
            if self._closing and not self._code_mode.pending_output_cells():
                return
            if redrive:
                await asyncio.sleep(0.1)
                self._cell_output_wake.set()
            elif self._code_mode.pending_output_cells():
                self._cell_output_wake.set()
                await asyncio.sleep(0)

    async def _settle_cell_output_failure(
        self, lease, candidate, error: Exception
    ) -> bool:
        """Separate a proven rejection from an ambiguous canonical write."""
        from pulsara_agent.code_mode.contracts import CodeModeError

        if lease is None:
            return False
        if candidate is None:
            if not isinstance(error, (TypeError, ValueError)):
                return False
        else:
            confirmation = await self._io.run(
                self.repository.confirm_cell_output_ingress,
                candidate=candidate,
                deadline_monotonic=self._canonical_deadline(),
            )
            if confirmation.kind is PromptIngressConfirmationKind.FULL_COMPATIBLE:
                await self._code_mode.acknowledge_output(lease)
                self._queue_wake.set()
                return True
            if confirmation.kind is not PromptIngressConfirmationKind.CONFLICT:
                if confirmation.kind is not PromptIngressConfirmationKind.NONE:
                    return False
                permanent = isinstance(
                    error, (TypeError, ValueError, ConversationKernelConflict)
                )
                if isinstance(error, PromptIngressRejected):
                    permanent = (
                        error.reason
                        is not PromptIngressWriteRejection.CAPACITY_EXHAUSTED
                    )
                if not permanent:
                    return False
        diagnostic = await self._code_mode.reject_output_delivery(
            lease, CodeModeError("output_delivery_rejected", str(error))
        )
        logging.getLogger("pulsara_agent.conversation_kernel.host").warning(
            "Cell output rejected cell=%s queue=%s source_exec_attempt=%s source_turn=%s record_sequence=%s reason=%s diagnostic=%s",
            lease.cell_id,
            None if candidate is None else candidate.queue_item_id,
            lease.source_exec_attempt_id,
            lease.source_turn_id,
            lease.record_sequence,
            error,
            diagnostic,
        )
        return True
