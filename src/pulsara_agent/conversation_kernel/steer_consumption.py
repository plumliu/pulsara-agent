"""Exact active-turn steer hydration and canonical settlement owner."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from pulsara_agent.conversation_kernel.contracts import WriterLease
from pulsara_agent.conversation_kernel.prompt_content import FrozenCanonicalPrompt
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
    KernelWatchdogOwner,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.process_local_settlement import (
    await_started_settlement,
)
from pulsara_agent.conversation_kernel.repository import (
    ConversationKernelConflict,
    ConversationKernelRepository,
    StaleHostWriter,
)
from pulsara_agent.conversation_kernel.steer import (
    MAXIMUM_STEER_CANDIDATE_EXPANDED_BYTES,
    AcceptedSteerDispatchEntry,
    PendingPromptSteerFact,
    PreparedSteerPlanConflictInterruption,
    PreparedSteerResourceRejection,
    PreparedSteerSuffixAdmissionPlan,
    SteerConsumptionConfirmationKind,
    SteerPlanConflictConfirmationKind,
    SteerResourceRejectionConfirmationKind,
    build_steer_plan_conflict_interruption,
    prepared_steer_suffix_plan_identity_fingerprint,
)
class PreparedSteerPlanStale(ConversationKernelConflict):
    """The quoted FIFO prefix stopped matching before its first mutation."""


class SteerConsumptionCoordinator:
    """Own hydration and exact settlement after one steer plan is admitted."""

    def __init__(
        self,
        *,
        repository: ConversationKernelRepository,
        writer_lease: WriterLease,
        io_owner: KernelSessionIO,
        deadline_factory: KernelExecutionDeadlineFactory,
    ) -> None:
        self._repository = repository
        self._writer_lease = writer_lease
        self._io = io_owner
        self._deadlines = deadline_factory

    def _canonical_deadline(self) -> float:
        return self._deadlines.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL)

    async def hydrate_pending(
        self,
        facts: tuple[PendingPromptSteerFact, ...],
        *,
        deadline: float,
    ) -> tuple[tuple[PendingPromptSteerFact, FrozenCanonicalPrompt], ...]:
        result: list[tuple[PendingPromptSteerFact, FrozenCanonicalPrompt]] = []
        used = 0
        for fact in facts:
            if (
                used + fact.canonical_expanded_bytes
                > MAXIMUM_STEER_CANDIDATE_EXPANDED_BYTES
            ):
                break
            prompt = await self._io.run(
                self._repository.hydrate_pending_prompt_steer,
                fact=fact,
                deadline_monotonic=deadline,
            )
            if (
                prompt.resource_quote.canonical_expanded_bytes
                > fact.canonical_expanded_bytes
            ):
                raise ConversationKernelConflict(
                    "pending steer expanded metadata underquoted hydration"
                )
            used += prompt.resource_quote.canonical_expanded_bytes
            result.append((fact, prompt))
        return tuple(result)

    async def consume_plan(
        self,
        plan: PreparedSteerSuffixAdmissionPlan,
    ) -> tuple[AcceptedSteerDispatchEntry, ...]:
        task = asyncio.create_task(
            self._consume_plan_worker(plan),
            name=(
                "kernel-steer-consumption:"
                f"{plan.selected_consumption_candidates[0].exact_target_turn_id}"
            ),
        )
        return await await_started_settlement(task)

    async def _consume_plan_worker(
        self,
        plan: PreparedSteerSuffixAdmissionPlan,
    ) -> tuple[AcceptedSteerDispatchEntry, ...]:
        accepted: list[AcceptedSteerDispatchEntry] = []
        for candidate in plan.selected_consumption_candidates:
            while True:
                try:
                    value = await self._io.run(
                        self._repository.consume_prepared_prompt_steer,
                        self._writer_lease.guard,
                        candidate=candidate,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                    accepted.append(value)
                    break
                except ConversationKernelConflict:
                    confirmation = await self._io.run(
                        self._repository.confirm_prepared_prompt_steer,
                        candidate=candidate,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                    if confirmation.kind is SteerConsumptionConfirmationKind.FULL:
                        assert confirmation.accepted is not None
                        accepted.append(confirmation.accepted)
                        break
                    if accepted:
                        await self.settle_plan_conflict(plan)
                        raise ConversationKernelConflict(
                            "prepared steer plan changed after partial consumption"
                        )
                    raise PreparedSteerPlanStale(
                        "prepared steer plan changed before first consumption"
                    )
                except BaseException:
                    confirmation = await self._io.run(
                        self._repository.confirm_prepared_prompt_steer,
                        candidate=candidate,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                    if confirmation.kind is SteerConsumptionConfirmationKind.FULL:
                        assert confirmation.accepted is not None
                        accepted.append(confirmation.accepted)
                        break
                    if confirmation.kind is SteerConsumptionConfirmationKind.NONE:
                        continue
                    if accepted:
                        await self.settle_plan_conflict(plan)
                    elif confirmation.kind is SteerConsumptionConfirmationKind.CONFLICT:
                        raise PreparedSteerPlanStale(
                            "prepared steer plan changed before first consumption"
                        )
                    raise ConversationKernelConflict(
                        "prepared steer consumption could not be settled"
                    )
        return tuple(accepted)

    async def settle_plan_conflict(
        self,
        plan: PreparedSteerSuffixAdmissionPlan,
    ) -> None:
        first = plan.selected_consumption_candidates[0]
        candidate = build_steer_plan_conflict_interruption(
            session_id=first.session_id,
            exact_target_turn_id=first.exact_target_turn_id,
            source_plan_fingerprint=prepared_steer_suffix_plan_identity_fingerprint(
                plan
            ),
            occurred_at=datetime.now(timezone.utc),
            actor_id=self._writer_lease.guard.writer_owner_id,
        )
        task = asyncio.create_task(
            self._settle_plan_conflict_worker(candidate),
            name=f"kernel-steer-plan-conflict:{candidate.exact_target_turn_id}",
        )
        await await_started_settlement(task)

    async def _settle_plan_conflict_worker(
        self,
        candidate: PreparedSteerPlanConflictInterruption,
    ) -> None:
        while True:
            try:
                await self._io.run(
                    self._repository.interrupt_prepared_steer_plan_conflict,
                    self._writer_lease.guard,
                    candidate=candidate,
                    deadline_monotonic=self._canonical_deadline(),
                )
            except StaleHostWriter:
                return
            except BaseException:
                pass
            try:
                confirmation = await self._io.run(
                    self._repository.confirm_prepared_steer_plan_conflict,
                    candidate=candidate,
                    deadline_monotonic=self._canonical_deadline(),
                )
            except BaseException:
                await asyncio.sleep(0.05)
                continue
            if confirmation.kind in {
                SteerPlanConflictConfirmationKind.FULL,
                SteerPlanConflictConfirmationKind.HISTORICAL_TERMINAL,
            }:
                return
            if confirmation.kind is SteerPlanConflictConfirmationKind.CONFLICT:
                raise ConversationKernelConflict(
                    "steer plan-conflict interruption has a foreign winner"
                )
            await asyncio.sleep(0.05)

    async def settle_resource_rejection(
        self,
        candidate: PreparedSteerResourceRejection,
    ) -> None:
        task = asyncio.create_task(
            self._settle_resource_rejection_worker(candidate),
            name=f"kernel-steer-resource-rejection:{candidate.exact_target_turn_id}",
        )
        await await_started_settlement(task)

    async def _settle_resource_rejection_worker(
        self,
        candidate: PreparedSteerResourceRejection,
    ) -> None:
        write_deadline = self._canonical_deadline()
        while True:
            try:
                await self._io.run(
                    self._repository.reject_prepared_prompt_steer_resource_exhaustion,
                    self._writer_lease.guard,
                    candidate=candidate,
                    deadline_monotonic=write_deadline,
                )
                return
            except StaleHostWriter:
                return
            except BaseException:
                try:
                    confirmation = await self._io.run(
                        self._repository.confirm_prepared_prompt_steer_resource_rejection,
                        session_id=self._writer_lease.guard.session_id,
                        candidate=candidate,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                except BaseException:
                    await asyncio.sleep(0.05)
                    write_deadline = self._canonical_deadline()
                    continue
                if confirmation.kind is SteerResourceRejectionConfirmationKind.FULL:
                    return
                if confirmation.kind is SteerResourceRejectionConfirmationKind.NONE:
                    write_deadline = self._canonical_deadline()
                    continue
                raise ConversationKernelConflict(
                    "steer resource rejection could not be settled"
                )


__all__ = ["PreparedSteerPlanStale", "SteerConsumptionCoordinator"]
