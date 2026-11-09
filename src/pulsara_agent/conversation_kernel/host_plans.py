"""Plan interaction admission, continuation, and successor settlement on the Host session."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime

from pulsara_agent.conversation_kernel.cancellation import (
    ActiveTurnCancellationIntent,
    ForegroundCancellationCause,
)
from pulsara_agent.conversation_kernel.compaction.runtime import (
    CompactionWriteReservation,
)
from pulsara_agent.conversation_kernel.contracts import canonical_digest
from pulsara_agent.conversation_kernel.prompt_content import freeze_canonical_prompt
from pulsara_agent.conversation_kernel.repository import (
    AcceptedPlanResolution,
    AcceptedPlanToolBatch,
    AcceptedPlanWorkflowCommand,
    ConversationKernelConflict,
    PlanContinuationDisposition,
    PlanContinuationInspection,
    PlanQuestionAnswer,
    PlanToolControlKind,
    PreparedPlanToolBatch,
    plan_draft_review_semantic_candidate,
    plan_exit_semantic_fingerprint,
    plan_question_resolution_semantic_fingerprint,
)
from pulsara_agent.conversation_kernel.steer import StartedTurnInputGroup
from pulsara_agent.identity import derive_id
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.plan_workflow import PlanDraftDecision, PlanHandoffKind

from .host_contracts import (
    KernelCommandOutcome,
    RootChainPhase,
    _PendingRootSuccessorHandoff,
)


class _HostPlanOperations:
    """Plan interaction admission, continuation, and successor settlement on the Host session.

    Operations share the concrete Host's existing state and admission locks.
    """

    def _admit_plan_resolution_write_locked(
        self, *, creates_turn: bool
    ) -> CompactionWriteReservation:
        """Linearize every new Plan-resolution write against force exit."""

        self._require_open()
        if self._manual_compaction_command_attempts:
            raise ConversationKernelConflict(
                "Plan resolution conflicts with idle compaction"
            )
        if self._plan_exit_fence:
            raise ConversationKernelConflict(
                "Plan resolution conflicts with force exit"
            )
        if self._compaction.is_fenced(
            scope_kind=self._session_scope_kind,
        ):
            raise ConversationKernelConflict(
                "Plan resolution conflicts with context compaction"
            )
        if creates_turn:
            self._retire_done_active_root_locked()
            if self._external_new_turn_accepting or self._active_task is not None:
                raise ConversationKernelConflict(
                    "a canonical ROOT turn is already running"
                )
        reservation = self._reserve_compaction_write_locked(
            scope_kind=self._session_scope_kind,
        )
        if creates_turn:
            self._external_new_turn_accepting = True
            self._external_new_turn_settled = asyncio.Event()
        return reservation

    async def _accept_automatic_plan_continuation(
        self,
        candidate: PreparedPlanToolBatch,
        deadline_monotonic: float,
    ) -> AcceptedPlanToolBatch:
        if candidate.continuation_turn_id is None:
            raise ValueError("automatic Plan continuation candidate has no turn")
        if candidate.continuation_entry_id is None:
            raise ValueError("automatic Plan continuation candidate has no entry")
        handoff_kind = (
            PlanHandoffKind.EXITED_PLAN
            if candidate.control_kind is PlanToolControlKind.EXIT
            else PlanHandoffKind.ENTERED_PLAN
        )
        origin_task = asyncio.current_task()
        if origin_task is None:
            raise RuntimeError("Plan continuation has no Host-owned origin task")

        attempt_id = ("plan-continuation", candidate.candidate_fingerprint)

        async def accept() -> AcceptedPlanToolBatch:
            try:
                result = await self._io.run(
                    self.repository.accept_plan_tool_batch,
                    self._lease.guard,
                    candidate=candidate,
                    deadline_monotonic=deadline_monotonic,
                )
            except Exception:
                winner = await self._io.run(
                    self.repository.confirm_plan_tool_batch_winner,
                    candidate=candidate,
                    deadline_monotonic=self._canonical_deadline(),
                )
                if winner is None:
                    raise
                result = winner
            try:
                inspection = await self._inspect_plan_continuation(
                    turn_id=candidate.continuation_turn_id,
                    entry_id=candidate.continuation_entry_id,
                    workflow_id=candidate.workflow_id,
                    interaction_id=None,
                    handoff_kind=handoff_kind,
                )
                disposition = self.repository.classify_plan_continuation(
                    inspection, self._lease.guard
                )
                if disposition is PlanContinuationDisposition.HISTORICAL_TERMINAL:
                    return replace(
                        result,
                        continuation_turn_id=None,
                        continuation_entry_id=None,
                    )
                started_group = await self._io.run(
                    self.repository.read_started_turn_input_group,
                    self._lease.guard,
                    turn_id=candidate.continuation_turn_id,
                    deadline_monotonic=self._canonical_deadline(),
                )
                async with self._lock:
                    current_writer = (
                        disposition
                        is PlanContinuationDisposition.RUNNING_CURRENT_WRITER
                    )
                    compatible_slot = (
                        self._active_task is origin_task
                        and self._active_turn_id == candidate.origin_turn_id
                    )
                    origin_runner_live = (
                        not origin_task.done()
                        and origin_task.cancelling() == 0
                        and self._active_cancellation_intent is not None
                        and self._active_cancellation_intent.cause is None
                    )
                    if (
                        not self._closing
                        and not self._plan_exit_fence
                        and current_writer
                        and compatible_slot
                        and origin_runner_live
                    ):
                        if self._pending_root_successor is not None:
                            raise ConversationKernelConflict(
                                "ROOT chain already owns a pending successor"
                            )
                        # The canonical successor is FULL, but the outer ROOT
                        # chain still executes the origin runner.  Preserve the
                        # origin cancellation intent until _finish_root_chain()
                        # atomically promotes this closed handoff.
                        self._pending_root_successor = _PendingRootSuccessorHandoff(
                            owner_task=origin_task,
                            attempt_id=attempt_id,
                            origin_turn_id=candidate.origin_turn_id,
                            successor_turn_id=candidate.continuation_turn_id,
                            successor_entry_id=candidate.continuation_entry_id,
                            workflow_id=candidate.workflow_id,
                            interaction_id=None,
                            handoff_kind=handoff_kind,
                            started_input_group=started_group,
                        )
                        self._active_root_phase = (
                            RootChainPhase.SUCCESSOR_COMMITTED_PENDING_HANDOFF
                        )
                        return result
            except BaseException:
                # The canonical continuation may already be FULL.  The same
                # process-local attempt must own its terminalization until the
                # exact turn is terminal or belongs to a replacement writer.
                pass
            await self._terminalize_unbound_plan_successor(
                attempt_id=attempt_id,
                turn_id=candidate.continuation_turn_id,
                entry_id=candidate.continuation_entry_id,
                workflow_id=candidate.workflow_id,
                interaction_id=None,
                handoff_kind=handoff_kind,
            )
            return replace(
                result,
                continuation_turn_id=None,
                continuation_entry_id=None,
            )

        async with self._lock:

            def require_new_attempt() -> None:
                if (
                    self._closing
                    or self._plan_exit_fence
                    or self._compaction.is_fenced(
                        scope_kind=self._session_scope_kind,
                    )
                    or self._active_task is not origin_task
                    or self._active_turn_id != candidate.origin_turn_id
                    or origin_task.done()
                    or origin_task.cancelling() != 0
                ):
                    raise ConversationKernelConflict(
                        "automatic Plan continuation admission is stale"
                    )

            attempt = self._plan_continuations.start(
                attempt_id=attempt_id,
                turn_id=candidate.continuation_turn_id,
                semantic_candidate_fingerprint=candidate.candidate_fingerprint,
                run=accept,
                before_start=require_new_attempt,
            )
        result = await asyncio.shield(attempt.task)
        if not isinstance(result, AcceptedPlanToolBatch):
            raise RuntimeError("Plan continuation owner returned an invalid result")
        return result

    async def enter_plan(
        self,
        *,
        command_id: str,
        entry_reason: str,
        resume_permission_mode: PermissionMode,
    ) -> KernelCommandOutcome:
        """Install a user-origin Plan workflow while the ROOT slot is idle."""

        workflow_id = derive_id("plan", "user", self.session_id, command_id)
        semantic_digest = canonical_digest(
            "pulsara:user-enter-plan:v1",
            {
                "workflow_id": workflow_id,
                "entry_reason": entry_reason,
                "resume_permission_mode": resume_permission_mode.value,
            },
        )
        existing = await self._query_command_row(command_id)
        if existing is not None:
            if (
                str(existing.get("command_kind")) != "ENTER_PLAN"
                or str(existing.get("semantic_digest")) != semantic_digest
                or str(existing.get("target_plan_workflow_id")) != workflow_id
            ):
                raise ConversationKernelConflict("Plan enter command conflicts")
            outcome = await self.query_command(command_id)
            assert outcome is not None
            return outcome
        async with self._lock:
            self._require_open()
            self._retire_done_active_root_locked()
            active = self._active_task
            if (
                self._plan_exit_fence
                or self._compaction.is_fenced(
                    scope_kind=self._session_scope_kind,
                )
                or self._external_new_turn_accepting
                or active is not None
            ):
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    workflow_id,
                    "ROOT_SLOT_UNAVAILABLE",
                    "Plan mode can only be entered while the ROOT slot is idle.",
                )
            self._external_new_turn_accepting = True
            self._external_new_turn_settled = asyncio.Event()
        try:
            accepted = await self._io.run(
                self.repository.enter_plan_by_user,
                self._lease.guard,
                command_id=command_id,
                workflow_id=workflow_id,
                entry_reason=entry_reason,
                resume_permission_mode=resume_permission_mode,
                occurred_at=datetime.now().astimezone(),
                actor_id=self.host_session_id,
                deadline_monotonic=self._canonical_deadline(),
            )
            return _plan_workflow_command_outcome(accepted)
        finally:
            await self._release_plan_continuation_reservation()

    async def cancel_plan(
        self,
        *,
        command_id: str,
        workflow_id: str,
        expected_workflow_revision: int,
    ) -> KernelCommandOutcome:
        return await self._exit_plan(
            command_id=command_id,
            command_kind="CANCEL_PLAN",
            workflow_id=workflow_id,
            expected_workflow_revision=expected_workflow_revision,
            force=False,
        )

    async def force_exit_plan(
        self,
        *,
        command_id: str,
        workflow_id: str,
        expected_workflow_revision: int,
    ) -> KernelCommandOutcome:
        return await self._exit_plan(
            command_id=command_id,
            command_kind="FORCE_EXIT_PLAN",
            workflow_id=workflow_id,
            expected_workflow_revision=expected_workflow_revision,
            force=True,
        )

    async def _exit_plan(
        self,
        *,
        command_id: str,
        command_kind: str,
        workflow_id: str,
        expected_workflow_revision: int,
        force: bool,
    ) -> KernelCommandOutcome:
        semantic_digest = plan_exit_semantic_fingerprint(
            command_kind=command_kind,
            workflow_id=workflow_id,
            expected_workflow_revision=expected_workflow_revision,
        )
        existing = await self._query_command_row(command_id)
        if existing is not None:
            if (
                str(existing.get("command_kind")) != command_kind
                or str(existing.get("semantic_digest")) != semantic_digest
                or str(existing.get("target_plan_workflow_id")) != workflow_id
            ):
                raise ConversationKernelConflict("Plan exit command conflicts")
            outcome = await self.query_command(command_id)
            assert outcome is not None
            return outcome
        async with self._lock:
            self._require_open()
            self._retire_done_active_root_locked()
            if (
                self._plan_exit_fence
                or self._compaction.is_fenced(
                    scope_kind=self._session_scope_kind,
                )
                or self._external_new_turn_accepting
            ):
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    workflow_id,
                    "PLAN_TRANSITION_BUSY",
                    "Another ROOT admission or Plan transition is in progress.",
                )
            active = self._active_task
            active_turn_id = self._active_turn_id
            if not force and active is not None:
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    workflow_id,
                    "PLAN_NOT_IDLE",
                    "Plan mode can only be cancelled while the ROOT slot is idle.",
                )
            self._plan_exit_fence = True
        try:
            if force:
                # Phase one validates the exact workflow/revision before any
                # physical cancellation.  The same transaction interrupts the
                # exact canonical ROOT turn and aborts its interaction.
                await self._io.run(
                    self.repository.prepare_force_plan_exit,
                    self._lease.guard,
                    command_id=command_id,
                    workflow_id=workflow_id,
                    expected_workflow_revision=expected_workflow_revision,
                    expected_active_turn_id=active_turn_id,
                    occurred_at=datetime.now().astimezone(),
                    actor_id=self.host_session_id,
                    deadline_monotonic=self._canonical_deadline(),
                )
                if active is not None:
                    active.cancel()
                    try:
                        await active
                    except BaseException:
                        pass
                    await self._settle_active_root_task(active)
                await self._plan_interactions.abort_current(
                    RuntimeError("Plan question was aborted by force exit")
                )
                # The origin task may have detached from a shielded automatic
                # continuation after that continuation committed FULL.  Keep
                # the exit fence installed until every already-admitted owner
                # has either bound before the fence or interrupted its exact
                # unbound successor.
                await self._plan_continuations.drain()
            accepted = await self._io.run(
                self.repository.exit_plan_by_user,
                self._lease.guard,
                command_id=command_id,
                command_kind=command_kind,
                workflow_id=workflow_id,
                expected_workflow_revision=expected_workflow_revision,
                occurred_at=datetime.now().astimezone(),
                actor_id=self.host_session_id,
                deadline_monotonic=self._canonical_deadline(),
            )
            return _plan_workflow_command_outcome(accepted)
        finally:
            async with self._lock:
                self._plan_exit_fence = False
                self._queue_wake.set()
                self._cell_output_wake.set()

    async def resolve_plan_question(
        self,
        *,
        command_id: str,
        workflow_id: str,
        expected_workflow_revision: int,
        interaction_id: str,
        answer: PlanQuestionAnswer | None,
        write_expected_writer_generation: int | None = None,
    ) -> AcceptedPlanResolution:
        self._require_open()
        result_id = derive_id("result", "plan-answer", self.session_id, command_id)
        result_entry_id = derive_id(
            "entry",
            "tool-result",
            derive_id("result", "plan-answer", self.session_id, command_id),
        )
        semantic_candidate_fingerprint = plan_question_resolution_semantic_fingerprint(
            workflow_id=workflow_id,
            expected_workflow_revision=expected_workflow_revision,
            interaction_id=interaction_id,
            answer=answer,
            result_id=result_id,
            result_entry_id=result_entry_id,
        )
        existing = await self._io.run(
            self.repository.confirm_plan_question_winner,
            session_id=self.session_id,
            command_id=command_id,
            workflow_id=workflow_id,
            expected_workflow_revision=expected_workflow_revision,
            interaction_id=interaction_id,
            answer=answer,
            result_id=result_id,
            result_entry_id=result_entry_id,
            deadline_monotonic=self._canonical_deadline(),
        )
        if existing is not None:
            await self._plan_interactions.settle(
                interaction_id=interaction_id,
                resolution=existing,
            )
            return existing
        if (
            write_expected_writer_generation is not None
            and write_expected_writer_generation != self.writer_generation
        ):
            raise ConversationKernelConflict(
                "Plan resolution writer generation is stale"
            )

        write_reservation: CompactionWriteReservation | None = None
        active_cancellation_intent: ActiveTurnCancellationIntent | None = None

        async def resolve() -> AcceptedPlanResolution:
            prepared = None
            publication_handle = None
            try:
                occurred_at = datetime.now().astimezone()
                provider_candidate = await self._io.run(
                    self.repository.prepare_plan_question_resolution_provider_input_candidate,
                    self._lease.guard,
                    workflow_id=workflow_id,
                    expected_workflow_revision=expected_workflow_revision,
                    interaction_id=interaction_id,
                    answer=answer,
                    result_id=result_id,
                    result_entry_id=result_entry_id,
                    occurred_at=occurred_at,
                    deadline_monotonic=self._canonical_deadline(),
                )
                assert write_reservation is not None
                prepared = await self._runner.prepare_plan_question_resolution_input(
                    provider_candidate,
                    cancellation_intent=active_cancellation_intent,
                    admitted_writer=write_reservation,
                )
                publication_handle = prepared.take_handle_for_publication()
                try:
                    resolution = await self._io.run(
                        self.repository.resolve_plan_question,
                        self._lease.guard,
                        command_id=command_id,
                        workflow_id=workflow_id,
                        expected_workflow_revision=expected_workflow_revision,
                        interaction_id=interaction_id,
                        answer=answer,
                        result_id=result_id,
                        result_entry_id=result_entry_id,
                        provider_input_admission=prepared.admission,
                        occurred_at=occurred_at,
                        actor_id=self.host_session_id,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                except Exception:
                    # The same stable semantic candidate is retried only after its
                    # stateless query-first path has checked for a FULL winner.
                    resolution = await self._io.run(
                        self.repository.resolve_plan_question,
                        self._lease.guard,
                        command_id=command_id,
                        workflow_id=workflow_id,
                        expected_workflow_revision=expected_workflow_revision,
                        interaction_id=interaction_id,
                        answer=answer,
                        result_id=result_id,
                        result_entry_id=result_entry_id,
                        provider_input_admission=prepared.admission,
                        occurred_at=occurred_at,
                        actor_id=self.host_session_id,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                await self._runner.confirm_published_active_root_input(
                    prepared,
                    publication_handle=publication_handle,
                    deadline_monotonic=self._canonical_deadline(),
                )
                publication_handle = None
                await self._plan_interactions.settle(
                    interaction_id=interaction_id,
                    resolution=resolution,
                )
                return resolution
            finally:
                if publication_handle is not None:
                    publication_handle.close()
                if prepared is not None:
                    prepared.close()
                if write_reservation is not None:
                    await self._release_compaction_write_reservation(write_reservation)

        def reserve_question_write() -> None:
            nonlocal write_reservation, active_cancellation_intent
            write_reservation = self._admit_plan_resolution_write_locked(
                creates_turn=False
            )
            active_cancellation_intent = self._active_cancellation_intent

        async with self._lock:
            attempt = self._plan_continuations.start(
                attempt_id=("plan-question-resolution", command_id),
                turn_id=None,
                semantic_candidate_fingerprint=semantic_candidate_fingerprint,
                run=resolve,
                before_start=reserve_question_write,
            )
        result = await asyncio.shield(attempt.task)
        if not isinstance(result, AcceptedPlanResolution):
            raise RuntimeError("Plan question resolution returned an invalid result")
        return result

    async def resolve_plan_draft_review(
        self,
        *,
        command_id: str,
        workflow_id: str,
        expected_workflow_revision: int,
        interaction_id: str,
        decision: PlanDraftDecision,
        revision_prompt: PromptContent | None = None,
        source_queue_item_id: str | None = None,
        write_expected_writer_generation: int | None = None,
    ) -> AcceptedPlanResolution:
        self._require_open()
        if revision_prompt is not None and source_queue_item_id is not None:
            raise ValueError("Plan revision must choose one input owner")
        canonical_prompt = None
        if source_queue_item_id is not None:
            canonical_prompt = await self._io.run(
                self.repository.read_plan_revision_queue_prompt,
                session_id=self.session_id,
                source_queue_item_id=source_queue_item_id,
                deadline_monotonic=self._canonical_deadline(),
            )
        elif revision_prompt is not None:
            frozen_content = await self._image_validator.freeze(
                revision_prompt,
                deadline_monotonic=self._canonical_deadline(),
            )
            canonical_prompt = freeze_canonical_prompt(
                frozen_content, workspace_id=self.workspace.workspace_key
            )
        creates_turn = decision in {
            PlanDraftDecision.APPROVE,
            PlanDraftDecision.REVISE,
        }
        continuation_turn_id = (
            derive_id("turn", "plan-review", self.session_id, command_id)
            if creates_turn
            else None
        )
        continuation_entry_id = (
            derive_id("entry", "plan-review", continuation_turn_id)
            if creates_turn
            else None
        )
        continuation_revision_id = (
            derive_id("binding", "revision", continuation_turn_id or "", "0")
            if creates_turn
            else None
        )
        (
            canonical_prompt,
            _,
            semantic_candidate_fingerprint,
        ) = plan_draft_review_semantic_candidate(
            workflow_id=workflow_id,
            expected_workflow_revision=expected_workflow_revision,
            interaction_id=interaction_id,
            decision=decision,
            canonical_prompt=canonical_prompt,
            source_queue_item_id=source_queue_item_id,
            continuation_turn_id=continuation_turn_id,
            continuation_entry_id=continuation_entry_id,
            continuation_context_binding_revision_id=continuation_revision_id,
        )
        existing = await self._io.run(
            self.repository.confirm_plan_draft_review_winner,
            session_id=self.session_id,
            command_id=command_id,
            workflow_id=workflow_id,
            expected_workflow_revision=expected_workflow_revision,
            interaction_id=interaction_id,
            decision=decision,
            canonical_prompt=canonical_prompt,
            source_queue_item_id=source_queue_item_id,
            continuation_turn_id=continuation_turn_id,
            continuation_entry_id=continuation_entry_id,
            continuation_context_binding_revision_id=continuation_revision_id,
            deadline_monotonic=self._canonical_deadline(),
        )
        if existing is not None:
            return existing
        if (
            write_expected_writer_generation is not None
            and write_expected_writer_generation != self.writer_generation
        ):
            raise ConversationKernelConflict(
                "Plan resolution writer generation is stale"
            )
        reserved = False
        write_reservation: CompactionWriteReservation | None = None
        attempt_id = ("plan-review-continuation", command_id)

        async def admit() -> AcceptedPlanResolution:
            try:
                occurred_at = datetime.now().astimezone()
                try:
                    resolution = await self._io.run(
                        self.repository.resolve_plan_draft_review,
                        self._lease.guard,
                        command_id=command_id,
                        workflow_id=workflow_id,
                        expected_workflow_revision=expected_workflow_revision,
                        interaction_id=interaction_id,
                        decision=decision,
                        canonical_prompt=canonical_prompt,
                        source_queue_item_id=source_queue_item_id,
                        continuation_turn_id=continuation_turn_id,
                        continuation_entry_id=continuation_entry_id,
                        continuation_context_binding_revision_id=(
                            continuation_revision_id
                        ),
                        occurred_at=occurred_at,
                        actor_id=self.host_session_id,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                except Exception:
                    resolution = await self._io.run(
                        self.repository.resolve_plan_draft_review,
                        self._lease.guard,
                        command_id=command_id,
                        workflow_id=workflow_id,
                        expected_workflow_revision=expected_workflow_revision,
                        interaction_id=interaction_id,
                        decision=decision,
                        canonical_prompt=canonical_prompt,
                        source_queue_item_id=source_queue_item_id,
                        continuation_turn_id=continuation_turn_id,
                        continuation_entry_id=continuation_entry_id,
                        continuation_context_binding_revision_id=(
                            continuation_revision_id
                        ),
                        occurred_at=occurred_at,
                        actor_id=self.host_session_id,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                self._queue_wake.set()
                if source_queue_item_id is not None:
                    self._hook_context.retire_prompt_candidate(
                        scope=self._hook_root_scope,
                        prompt_candidate_id=source_queue_item_id,
                    )
                if resolution.continuation_turn_id is None:
                    if reserved:
                        await self._release_plan_continuation_reservation()
                    return resolution
                assert resolution.continuation_entry_id is not None
                handoff_kind = (
                    PlanHandoffKind.APPROVED_PLAN
                    if decision is PlanDraftDecision.APPROVE
                    else PlanHandoffKind.REVISION_REQUESTED
                )
                try:
                    inspection = await self._inspect_plan_continuation(
                        turn_id=resolution.continuation_turn_id,
                        entry_id=resolution.continuation_entry_id,
                        workflow_id=workflow_id,
                        interaction_id=interaction_id,
                        handoff_kind=handoff_kind,
                    )
                    disposition = self.repository.classify_plan_continuation(
                        inspection, self._lease.guard
                    )
                    if disposition is PlanContinuationDisposition.HISTORICAL_TERMINAL:
                        await self._release_plan_continuation_reservation()
                        return resolution
                    started_group = await self._io.run(
                        self.repository.read_started_turn_input_group,
                        self._lease.guard,
                        turn_id=resolution.continuation_turn_id,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                    if await self._bind_plan_review_successor(
                        command_id=command_id,
                        inspection=inspection,
                        started_input_group=started_group,
                    ):
                        return resolution
                except BaseException:
                    # FULL canonical acceptance already owns the successor;
                    # this attempt must continue into terminalization.
                    pass
                await self._terminalize_unbound_plan_successor(
                    attempt_id=attempt_id,
                    turn_id=resolution.continuation_turn_id,
                    entry_id=resolution.continuation_entry_id,
                    workflow_id=workflow_id,
                    interaction_id=interaction_id,
                    handoff_kind=handoff_kind,
                )
                await self._release_plan_continuation_reservation()
                return resolution
            except BaseException:
                if reserved:
                    await self._release_plan_continuation_reservation()
                raise
            finally:
                if write_reservation is not None:
                    await self._release_compaction_write_reservation(write_reservation)

        async with self._lock:

            def reserve_new_attempt() -> None:
                nonlocal reserved, write_reservation
                write_reservation = self._admit_plan_resolution_write_locked(
                    creates_turn=creates_turn
                )
                reserved = creates_turn
                if creates_turn:
                    self._external_new_turn_cancellation_intent = (
                        ActiveTurnCancellationIntent(
                            continuation_turn_id,
                            self._session_scope_kind,
                        )
                    )

            attempt = self._plan_continuations.start(
                attempt_id=attempt_id,
                turn_id=continuation_turn_id,
                semantic_candidate_fingerprint=semantic_candidate_fingerprint,
                run=admit,
                before_start=reserve_new_attempt,
            )
        result = await asyncio.shield(attempt.task)
        if not isinstance(result, AcceptedPlanResolution):
            raise RuntimeError("Plan review admission returned an invalid result")
        return result

    async def _inspect_plan_continuation(
        self,
        *,
        turn_id: str,
        entry_id: str,
        workflow_id: str,
        interaction_id: str | None,
        handoff_kind: PlanHandoffKind,
    ) -> PlanContinuationInspection:
        inspection = await self._io.run(
            self.repository.inspect_plan_continuation,
            session_id=self.session_id,
            turn_id=turn_id,
            initial_entry_id=entry_id,
            workflow_id=workflow_id,
            interaction_id=interaction_id,
            handoff_kind=handoff_kind,
            deadline_monotonic=self._canonical_deadline(),
        )
        if inspection is None:
            raise ConversationKernelConflict("Plan continuation winner is absent")
        return inspection

    def _inspection_owned_by_current_host(
        self, inspection: PlanContinuationInspection
    ) -> bool:
        return (
            self.repository.classify_plan_continuation(inspection, self._lease.guard)
            is PlanContinuationDisposition.RUNNING_CURRENT_WRITER
        )

    async def _bind_plan_review_successor(
        self,
        *,
        command_id: str,
        inspection: PlanContinuationInspection,
        started_input_group: StartedTurnInputGroup | None,
    ) -> bool:
        async with self._lock:
            self._retire_done_active_root_locked()
            if (
                self._closing
                or self._plan_exit_fence
                or not self._inspection_owned_by_current_host(inspection)
                or not self._external_new_turn_accepting
                or self._active_task is not None
                or (
                    self._external_new_turn_cancellation_intent is not None
                    and self._external_new_turn_cancellation_intent.cause is not None
                )
            ):
                return False
            self._tools.current_work_owner.bind_continuation_if_present(
                scope_kind=self._session_scope_kind,
                turn_id=inspection.turn_id,
            )
            self._install_active_root_task_locked(
                turn_id=inspection.turn_id,
                command_id=command_id,
                name=f"kernel-plan-review-turn:{inspection.turn_id}",
                cancellation_intent=self._external_new_turn_cancellation_intent,
                run=lambda intent: self._run_accepted_root_chain(
                    inspection.turn_id,
                    intent,
                    started_input_group=started_input_group,
                ),
            )
            self._external_new_turn_accepting = False
            self._external_new_turn_cancellation_intent = None
            self._external_new_turn_settled.set()
            return True

    def _starting_turn_cancellation_cause(self, turn_id: str):
        external = self._external_new_turn_cancellation_intent
        if external is not None and external.turn_id == turn_id:
            return external.cause
        active = self._active_cancellation_intent
        return None if active is None else active.cause

    async def _terminalize_unbound_plan_successor(
        self,
        *,
        attempt_id: str,
        turn_id: str,
        entry_id: str,
        workflow_id: str,
        interaction_id: str | None,
        handoff_kind: PlanHandoffKind,
    ) -> None:
        """Retain ownership until the exact FULL successor is safely settled."""

        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Plan successor terminalization has no task owner")
        self._plan_continuations.mark_terminalizing(
            attempt_id=attempt_id,
            task=task,
        )
        await self._terminalize_plan_successor_until_safe(
            turn_id=turn_id,
            entry_id=entry_id,
            workflow_id=workflow_id,
            interaction_id=interaction_id,
            handoff_kind=handoff_kind,
            reason=(
                "USER_STOPPED"
                if self._starting_turn_cancellation_cause(turn_id)
                is ForegroundCancellationCause.USER_REQUEST
                else "SESSION_CLOSED"
                if self._closing
                or self._starting_turn_cancellation_cause(turn_id)
                is ForegroundCancellationCause.HOST_SESSION_CLOSE
                else "PLAN_CONTINUATION_NOT_BOUND"
            ),
        )

    async def _terminalize_pending_root_successor(
        self, pending: _PendingRootSuccessorHandoff, *, reason: str
    ) -> None:
        await self._terminalize_plan_successor_until_safe(
            turn_id=pending.successor_turn_id,
            entry_id=pending.successor_entry_id,
            workflow_id=pending.workflow_id,
            interaction_id=pending.interaction_id,
            handoff_kind=pending.handoff_kind,
            reason=reason,
        )

    async def _terminalize_plan_successor_until_safe(
        self,
        *,
        turn_id: str,
        entry_id: str,
        workflow_id: str,
        interaction_id: str | None,
        handoff_kind: PlanHandoffKind,
        reason: str,
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
            except asyncio.CancelledError:
                raise
            except Exception:
                # Confirmation below distinguishes a replacement writer from
                # a transient failure under this exact writer.
                pass
            try:
                inspection = await self._inspect_plan_continuation(
                    turn_id=turn_id,
                    entry_id=entry_id,
                    workflow_id=workflow_id,
                    interaction_id=interaction_id,
                    handoff_kind=handoff_kind,
                )
                disposition = self.repository.classify_plan_continuation(
                    inspection, self._lease.guard
                )
                if disposition in {
                    PlanContinuationDisposition.HISTORICAL_TERMINAL,
                    PlanContinuationDisposition.NOT_OWNED_BY_CURRENT_WRITER,
                }:
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(delay_seconds)
            delay_seconds = min(delay_seconds * 2, 0.5)

    async def _release_plan_continuation_reservation(self) -> None:
        async with self._lock:
            self._external_new_turn_accepting = False
            self._external_new_turn_cancellation_intent = None
            self._external_new_turn_settled.set()


def _plan_workflow_command_outcome(
    accepted: AcceptedPlanWorkflowCommand,
) -> KernelCommandOutcome:
    code = {
        "ACTIVE": "PLAN_ENTERED",
        "CANCELLED": "PLAN_CANCELLED",
        "FORCE_EXITED": "PLAN_FORCE_EXITED",
    }[accepted.workflow_status.value]
    return KernelCommandOutcome(
        command_id=accepted.command_id,
        status="SUCCEEDED",
        target_id=accepted.workflow_id,
        public_code=code,
        public_message="Plan workflow transition accepted.",
        plan_workflow_status=accepted.workflow_status.value,
        resume_permission_mode=accepted.resume_permission_mode,
        handoff_created_at_commit=accepted.handoff_created_at_commit,
        plan_workflow_revision=accepted.workflow_revision,
    )
