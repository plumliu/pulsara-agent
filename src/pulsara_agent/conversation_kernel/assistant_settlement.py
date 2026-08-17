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
    AssistantDataBlock,
    AssistantTextBlock,
    ConversationKernelConflict,
    ConversationKernelRepository,
    StaleHostWriter,
)
from pulsara_agent.llm.request import ProviderAssistantReplayFragment
from pulsara_agent.model_input.contracts import PreparedProviderInputCut
from pulsara_agent.model_input.continuity import ProviderInputContinuityScope
from pulsara_agent.primitives.context import context_fingerprint, thaw_json


MAXIMUM_ASSISTANT_SETTLEMENT_WRITE_CONFIRM_ATTEMPTS = 4


class AssistantMessageSettlementAbandoned(RuntimeError):
    """The finite process-local settlement policy found no exact winner."""


@dataclass(frozen=True, slots=True)
class PreparedAssistantMessageSettlement:
    candidate_fingerprint: str
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
    replay_fragment: ProviderAssistantReplayFragment | None = field(
        default=None, repr=False
    )

    def __post_init__(self) -> None:
        if (
            not self.candidate_fingerprint.startswith("sha256:")
            or not self.entry_id
            or not self.actor_id
            or not self.continuity_epoch_nonce
            or self.continuity_epoch_revision < 1
            or self.guard.session_id != self.cut.session_id
            or self.continuity_scope.session_id != self.cut.session_id
        ):
            raise ValueError("assistant settlement candidate is invalid")
        if self.replay_fragment is not None and (
            self.replay_fragment.assistant_entry_id != self.entry_id
        ):
            raise ValueError("assistant replay fragment targets another entry")
        expected = assistant_settlement_candidate_fingerprint(
            cut=self.cut,
            entry_id=self.entry_id,
            parent_content=self.parent_content,
            blocks=self.blocks,
            complete_turn=self.complete_turn,
            occurred_at=self.occurred_at,
            actor_id=self.actor_id,
            continuity_scope=self.continuity_scope,
            continuity_epoch_nonce=self.continuity_epoch_nonce,
            continuity_epoch_revision=self.continuity_epoch_revision,
            replay_fragment=self.replay_fragment,
        )
        if self.candidate_fingerprint != expected:
            raise ValueError("assistant settlement candidate fingerprint mismatch")


def assistant_settlement_candidate_fingerprint(
    *,
    cut: PreparedProviderInputCut,
    entry_id: str,
    parent_content: CanonicalContent,
    blocks: tuple[AssistantBlock, ...],
    complete_turn: bool,
    occurred_at: datetime,
    actor_id: str,
    continuity_scope: ProviderInputContinuityScope,
    continuity_epoch_nonce: str,
    continuity_epoch_revision: int,
    replay_fragment: ProviderAssistantReplayFragment | None,
) -> str:
    def content_value(value: CanonicalContent) -> tuple[object, ...]:
        return (
            type(value).__name__,
            getattr(value, "blob_id", None),
            value.digest,
            value.size,
            value.media_type,
            value.codec,
        )

    block_values: list[tuple[object, ...]] = []
    for block in blocks:
        if isinstance(block, AssistantTextBlock):
            block_values.append(("TEXT", block.block_id, content_value(block.text)))
        elif isinstance(block, AssistantDataBlock):
            block_values.append(("DATA", block.block_id, content_value(block.data)))
        else:
            block_values.append(
                (
                    "TOOL_CALL",
                    block.block_id,
                    block.tool_call_id,
                    block.tool_name,
                    context_fingerprint(
                        "pulsara:runner-frozen-json:v1",
                        thaw_json(block.arguments),
                    ),
                )
            )
    return context_fingerprint(
        "pulsara.prepared-assistant-message-settlement:v2",
        {
            "cut": (
                cut.session_id,
                cut.turn_id,
                cut.context_binding_revision_id,
                cut.provider_input_through_sequence,
            ),
            "entry": entry_id,
            "parent": content_value(parent_content),
            "blocks": tuple(block_values),
            "complete_turn": complete_turn,
            "occurred_at": occurred_at.isoformat(),
            "actor": actor_id,
            "continuity": (
                continuity_scope.session_id,
                continuity_scope.scope_kind.value,
                continuity_scope.scope_subagent_task_id,
                continuity_epoch_nonce,
                continuity_epoch_revision,
            ),
            "replay": (
                None
                if replay_fragment is None
                else replay_fragment.fragment_fingerprint
            ),
        },
    )


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

    async def settle(
        self, candidate: PreparedAssistantMessageSettlement
    ) -> AcceptedEntry:
        async with self._lock:
            if self._closed:
                raise RuntimeError("assistant settlement owner is closed")
            current = self._attempts.get(candidate.entry_id)
            if current is not None:
                if (
                    current.candidate.candidate_fingerprint
                    != candidate.candidate_fingerprint
                    or current.candidate.guard != candidate.guard
                ):
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
        reservation: ProcessLocalAssistantReplayFragmentReservation | None = None
        if candidate.replay_fragment is not None:
            # This is the last fallible capacity gate.  It runs before any
            # canonical assistant mutation and remains charged across
            # ACK-unknown confirmation/reissue of the same stable candidate.
            reservation = self._continuity.reserve_assistant_replay_fragment(
                scope=candidate.continuity_scope,
                epoch_nonce=candidate.continuity_epoch_nonce,
                epoch_revision=candidate.continuity_epoch_revision,
                fragment=candidate.replay_fragment,
            )
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
                        complete_turn=candidate.complete_turn,
                        occurred_at=candidate.occurred_at,
                        actor_id=candidate.actor_id,
                        deadline_monotonic=self._deadline(),
                    )
                    break
                except StaleHostWriter:
                    raise
                except ConversationKernelConflict:
                    self._discard_scope_after_unbound(candidate)
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
                            complete_turn=candidate.complete_turn,
                            occurred_at=candidate.occurred_at,
                            actor_id=candidate.actor_id,
                            deadline_monotonic=self._deadline(),
                        )
                    except StaleHostWriter:
                        raise
                    except ConversationKernelConflict:
                        self._discard_scope_after_unbound(candidate)
                        raise
                    except BaseException:
                        if self._closed or attempt + 1 >= (
                            MAXIMUM_ASSISTANT_SETTLEMENT_WRITE_CONFIRM_ATTEMPTS
                        ):
                            self._discard_scope_after_unbound(candidate)
                            raise AssistantMessageSettlementAbandoned(
                                "assistant winner could not be confirmed"
                            )
                        await asyncio.sleep(0.05)
                        continue
                    if accepted is None:
                        if self._closed or attempt + 1 >= (
                            MAXIMUM_ASSISTANT_SETTLEMENT_WRITE_CONFIRM_ATTEMPTS
                        ):
                            self._discard_scope_after_unbound(candidate)
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
            if candidate.replay_fragment is not None:
                # A canonical assistant winner without its required opaque
                # carrier cannot remain in the same strict-prefix epoch.  A
                # cold, process-local scope reset is the only safe fallback;
                # it neither rewrites the winner nor persists replay state.
                self._discard_scope_after_unbound(candidate)
            raise

    def _deadline(self) -> float:
        return self._deadlines.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL)

    def _discard_scope_after_unbound(
        self, candidate: PreparedAssistantMessageSettlement
    ) -> None:
        try:
            self._continuity.discard_scope(candidate.continuity_scope)
        except BaseException:
            # Scope close/takeover is already a cold-epoch boundary.  This is
            # local cleanup only and must never mask the settlement outcome.
            pass

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
    "assistant_settlement_candidate_fingerprint",
]
