"""Host-owned process-local settlement for one complete assistant response.

The owner exists only to keep an already-completed provider response attached
through caller cancellation and an ambiguous PostgreSQL acknowledgement.  It
does not persist work, retry provider execution, or create a receipt graph.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic

from pulsara_agent.conversation_kernel.contracts import (
    CanonicalContent,
    HostWriterGuard,
)
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
    KernelWatchdogOwner,
)
from pulsara_agent.conversation_kernel.input_continuity import (
    HostProviderInputContinuityOwner,
    ProcessLocalAssistantReplayFragmentReservation,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.repository import (
    AcceptedEntry,
    AssistantBlock,
    ConversationKernelConflict,
    ConversationKernelRepository,
    StaleHostWriter,
)
from pulsara_agent.llm.provider_replay import (
    PreparedDurableProviderAssistantReplay,
)
from pulsara_agent.model_input.contracts import PreparedProviderInputCut
from pulsara_agent.model_input.continuity import ProviderInputContinuityScope
from pulsara_agent.conversation_kernel.subagents.contracts import (
    FrozenSubagentResultPublicFact,
)
from pulsara_agent.conversation_kernel.visualization import FrozenVisualizationOccurrence


MAXIMUM_ASSISTANT_SETTLEMENT_WRITE_CONFIRM_ATTEMPTS = 4


class AssistantMessageSettlementAbandoned(RuntimeError):
    """The finite process-local settlement policy found no exact winner."""


@dataclass(frozen=True, slots=True)
class PreparedAssistantMessageSettlement:
    guard: HostWriterGuard
    cut: PreparedProviderInputCut
    entry_id: str
    parent_content: CanonicalContent = field(repr=False)
    blocks: tuple[AssistantBlock, ...] = field(repr=False)
    complete_turn: bool
    occurred_at: datetime
    actor_id: str
    continuity_scope: ProviderInputContinuityScope
    continuity_epoch_nonce: str
    continuity_epoch_revision: int
    visualizations: tuple[FrozenVisualizationOccurrence, ...] = field(
        default=(), repr=False
    )
    provider_replay: PreparedDurableProviderAssistantReplay | None = field(
        default=None, repr=False
    )
    provider_replay_reservation: (
        ProcessLocalAssistantReplayFragmentReservation | None
    ) = field(default=None, repr=False, compare=False)
    subagent_result: FrozenSubagentResultPublicFact | None = field(
        default=None, repr=False
    )

    def __post_init__(self) -> None:
        if (
            not self.entry_id
            or not self.actor_id
            or not self.continuity_epoch_nonce
            or self.continuity_epoch_revision < 1
            or self.guard.session_id != self.cut.session_id
            or self.continuity_scope.session_id != self.cut.session_id
        ):
            raise ValueError("assistant settlement candidate is invalid")
        if self.provider_replay is not None and (
            self.provider_replay.assistant_entry_id != self.entry_id
        ):
            raise ValueError("assistant replay composite is invalid")
        if (self.provider_replay is not None) != (
            self.provider_replay_reservation is not None
        ):
            raise ValueError("assistant replay reservation union is invalid")
        if self.provider_replay_reservation is not None:
            reservation = self.provider_replay_reservation
            assert self.provider_replay is not None
            if (
                reservation.scope != self.continuity_scope
                or reservation.epoch_nonce != self.continuity_epoch_nonce
                or reservation.epoch_revision != self.continuity_epoch_revision
                or reservation.fragment != self.provider_replay.fragment()
            ):
                raise ValueError("assistant replay reservation does not exact-join")
        if self.subagent_result is not None and (
            not self.complete_turn
            or self.continuity_scope.scope_kind.value != "SUBAGENT_TASK"
            or self.continuity_scope.scope_subagent_task_id
            != self.subagent_result.task_id
            or self.subagent_result.producer_entry_id != self.entry_id
            or self.subagent_result.source.value != "INFERRED"
        ):
            raise ValueError("inferred subagent result does not exact-join assistant")


@dataclass(slots=True)
class _Attempt:
    candidate: PreparedAssistantMessageSettlement
    task: asyncio.Task[AcceptedEntry]


class AssistantMessageSettlementOwner:
    """Shield and exact-confirm complete assistant candidates for one Host."""

    def __init__(
        self,
        *,
        repository: ConversationKernelRepository,
        io_owner: KernelSessionIO,
        continuity_owner: HostProviderInputContinuityOwner,
        deadline_factory: KernelExecutionDeadlineFactory,
    ) -> None:
        self._repository = repository
        self._io = io_owner
        self._continuity = continuity_owner
        self._deadlines = deadline_factory
        self._lock = asyncio.Lock()
        self._attempts: dict[str, _Attempt] = {}
        self._closed = False

    def prepare_replay_reservation(
        self,
        *,
        scope: ProviderInputContinuityScope,
        epoch_nonce: str,
        epoch_revision: int,
        provider_replay: PreparedDurableProviderAssistantReplay,
    ) -> ProcessLocalAssistantReplayFragmentReservation:
        """Check replay capacity before Hook/effects; the caller owns it until settle."""

        return self._continuity.reserve_assistant_replay_fragment(
            scope=scope,
            epoch_nonce=epoch_nonce,
            epoch_revision=epoch_revision,
            fragment=provider_replay.fragment(),
        )

    async def settle(
        self, candidate: PreparedAssistantMessageSettlement
    ) -> AcceptedEntry:
        async with self._lock:
            if self._closed:
                if candidate.provider_replay_reservation is not None:
                    self._continuity.release_assistant_replay_fragment_reservation(
                        candidate.provider_replay_reservation
                    )
                raise RuntimeError("assistant settlement owner is closed")
            current = self._attempts.get(candidate.entry_id)
            if current is not None:
                if current.candidate is not candidate:
                    if candidate.provider_replay_reservation is not None:
                        self._continuity.release_assistant_replay_fragment_reservation(
                            candidate.provider_replay_reservation
                        )
                    raise ConversationKernelConflict(
                        "assistant settlement candidate identity conflicts"
                    )
                task = current.task
            else:
                task = asyncio.create_task(
                    self._settle_worker(candidate),
                    name=f"kernel-assistant-settlement:{candidate.entry_id}",
                )
                self._attempts[candidate.entry_id] = _Attempt(candidate, task)
                task.add_done_callback(
                    lambda completed, entry_id=candidate.entry_id: (
                        self._retire_done(entry_id, completed)
                    )
                )
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
                continue
            except BaseException:
                break
        result = task.result()
        if cancellation is not None:
            raise cancellation
        return result

    async def aclose(self, *, deadline_monotonic: float) -> None:
        async with self._lock:
            self._closed = True
            tasks = tuple(item.task for item in self._attempts.values())
        deadline_expired = False
        cancellation: asyncio.CancelledError | None = None
        first_error: BaseException | None = None
        for task in tasks:
            while not task.done():
                remaining = deadline_monotonic - monotonic()
                if remaining > 0 and not deadline_expired:
                    try:
                        done, _pending = await asyncio.wait(
                            (task,), timeout=remaining
                        )
                    except asyncio.CancelledError as exc:
                        cancellation = cancellation or exc
                        continue
                    if not done:
                        deadline_expired = True
                    continue
                deadline_expired = True
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError as exc:
                    cancellation = cancellation or exc
                    continue
                except BaseException:
                    break
            try:
                task.result()
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
            except AssistantMessageSettlementAbandoned:
                # Final NONE/unknown during Host close deliberately discards
                # the process-local fragment and lets the next Host cold-read
                # canonical truth.  It is a terminal settlement disposition,
                # not a physical close failure.
                pass
            except BaseException as exc:
                first_error = first_error or exc
        if cancellation is not None:
            raise cancellation
        if deadline_expired:
            raise TimeoutError("assistant settlement drained after close deadline")
        if first_error is not None:
            raise first_error

    async def _settle_worker(
        self, candidate: PreparedAssistantMessageSettlement
    ) -> AcceptedEntry:
        # K3 admits native replay before any canonical publication, Hook or
        # effect.  This owner receives that exact reservation and only promotes
        # it after the canonical assistant winner is FULL.
        reservation = candidate.provider_replay_reservation
        try:
            for attempt in range(
                MAXIMUM_ASSISTANT_SETTLEMENT_WRITE_CONFIRM_ATTEMPTS
            ):
                try:
                    accepted = await self._io.run(
                        self._repository.commit_assistant_message,
                        candidate.guard,
                        cut=candidate.cut,
                        entry_id=candidate.entry_id,
                        parent_content=candidate.parent_content,
                        blocks=candidate.blocks,
                        visualizations=candidate.visualizations,
                        provider_replay=candidate.provider_replay,
                        subagent_result=candidate.subagent_result,
                        complete_turn=candidate.complete_turn,
                        occurred_at=candidate.occurred_at,
                        actor_id=candidate.actor_id,
                        deadline_monotonic=self._deadline(),
                    )
                    break
                except StaleHostWriter:
                    raise
                except ConversationKernelConflict:
                    raise
                except BaseException:
                    try:
                        accepted = await self._io.run(
                            self._repository.confirm_assistant_message_winner,
                            candidate.guard,
                            cut=candidate.cut,
                            entry_id=candidate.entry_id,
                            parent_content=candidate.parent_content,
                            blocks=candidate.blocks,
                            visualizations=candidate.visualizations,
                            provider_replay=candidate.provider_replay,
                            subagent_result=candidate.subagent_result,
                            complete_turn=candidate.complete_turn,
                            occurred_at=candidate.occurred_at,
                            actor_id=candidate.actor_id,
                            deadline_monotonic=self._deadline(),
                        )
                    except StaleHostWriter:
                        raise
                    except ConversationKernelConflict:
                        raise
                    except BaseException:
                        if self._closed or attempt + 1 >= (
                            MAXIMUM_ASSISTANT_SETTLEMENT_WRITE_CONFIRM_ATTEMPTS
                        ):
                            raise AssistantMessageSettlementAbandoned(
                                "assistant winner could not be confirmed"
                            )
                        await asyncio.sleep(0.05)
                        continue
                    if accepted is None:
                        if self._closed or attempt + 1 >= (
                            MAXIMUM_ASSISTANT_SETTLEMENT_WRITE_CONFIRM_ATTEMPTS
                        ):
                            raise AssistantMessageSettlementAbandoned(
                                "assistant settlement reached terminal NONE"
                            )
                        await asyncio.sleep(0)
                        continue
                    break
            else:  # pragma: no cover - each terminal branch exits explicitly
                raise AssertionError("assistant settlement loop did not terminate")
            if reservation is not None:
                self._continuity.promote_assistant_replay_fragment(reservation)
                reservation = None
            return accepted
        except BaseException:
            if reservation is not None:
                try:
                    self._continuity.release_assistant_replay_fragment_reservation(
                        reservation
                    )
                except BaseException:
                    # Scope close/takeover may already have retired the claim.
                    # Reservation cleanup must not mask the canonical outcome.
                    pass
            # Settlement failure is not an epoch boundary. The installed
            # prefix remains authoritative until cold bootstrap or adopted
            # compaction explicitly replaces it.
            raise

    def _deadline(self) -> float:
        return self._deadlines.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL)

    def _retire_done(
        self, entry_id: str, completed: asyncio.Task[AcceptedEntry]
    ) -> None:
        async def retire() -> None:
            async with self._lock:
                current = self._attempts.get(entry_id)
                if current is not None and current.task is completed:
                    self._attempts.pop(entry_id, None)

        try:
            asyncio.get_running_loop().create_task(retire())
        except RuntimeError:
            return


__all__ = [
    "AssistantMessageSettlementAbandoned",
    "AssistantMessageSettlementOwner",
    "MAXIMUM_ASSISTANT_SETTLEMENT_WRITE_CONFIRM_ATTEMPTS",
    "PreparedAssistantMessageSettlement",
]
