"""Process-local producer gate for durable transcript checkpoint barriers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from threading import Condition, RLock
from time import monotonic
from uuid import uuid4

from pulsara_agent.primitives.authority_materialization import (
    CheckpointDispatchBarrierFact,
    LedgerWriteAdmissionClass,
)


class CheckpointDispatchGateClosed(RuntimeError):
    """A producer attempted to enter a ledger owned by a checkpoint barrier."""


class CheckpointDispatchDrainTimeout(RuntimeError):
    """Previously admitted producer writes did not leave before the deadline."""


class CheckpointDispatchGateState(StrEnum):
    OPEN = "open"
    DRAINING = "draining"
    ACTIVE = "active"
    RECONCILIATION_REQUIRED = "reconciliation_required"


@dataclass(frozen=True, slots=True)
class ProducerAdmissionToken:
    token_id: str
    generation: int
    operation_owner_id: str | None = None


@dataclass(frozen=True, slots=True)
class CheckpointDrainToken:
    checkpoint_id: str
    checkpoint_candidate_fingerprint: str
    generation: int


class CheckpointDispatchBarrierCoordinator:
    """Linearize producer admission around one durable checkpoint owner.

    AP3 tokens cover the complete physical event-writer batch. AP4 extends the
    same coordinator token across the complete model/tool/child operation.
    """

    def __init__(
        self,
        *,
        active_barrier: CheckpointDispatchBarrierFact | None = None,
    ) -> None:
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._generation = 0
        self._active_producers: dict[str, ProducerAdmissionToken] = {}
        self._checkpoint_id: str | None = None
        self._checkpoint_candidate_fingerprint: str | None = None
        if active_barrier is None:
            self._state = CheckpointDispatchGateState.OPEN
        else:
            self._state = CheckpointDispatchGateState.ACTIVE
            self._checkpoint_id = active_barrier.checkpoint_id
            self._checkpoint_candidate_fingerprint = (
                active_barrier.checkpoint_candidate_fingerprint
            )

    @property
    def state(self) -> CheckpointDispatchGateState:
        with self._lock:
            return self._state

    @property
    def active_producer_count(self) -> int:
        with self._lock:
            return len(self._active_producers)

    def acquire_write_admission(
        self,
        *,
        admission_class: LedgerWriteAdmissionClass,
        checkpoint_id: str | None = None,
        operation_owner_id: str | None = None,
    ) -> ProducerAdmissionToken | None:
        """Admit one writer batch or validate an exact control owner."""

        with self._lock:
            if admission_class is LedgerWriteAdmissionClass.PRODUCER:
                if self._state is not CheckpointDispatchGateState.OPEN:
                    raise CheckpointDispatchGateClosed(
                        "checkpoint barrier rejects new producer admission"
                    )
                token = ProducerAdmissionToken(
                    token_id=f"checkpoint-producer:{uuid4().hex}",
                    generation=self._generation,
                    operation_owner_id=None,
                )
                self._active_producers[token.token_id] = token
                return token

            if admission_class is LedgerWriteAdmissionClass.OPERATION_CONTINUATION:
                if not operation_owner_id or not any(
                    item.operation_owner_id == operation_owner_id
                    for item in self._active_producers.values()
                ):
                    raise CheckpointDispatchGateClosed(
                        "operation continuation has no exact admitted owner"
                    )
                if self._state not in {
                    CheckpointDispatchGateState.OPEN,
                    CheckpointDispatchGateState.DRAINING,
                }:
                    raise CheckpointDispatchGateClosed(
                        "operation continuation conflicts with active checkpoint"
                    )
                return None

            if admission_class is LedgerWriteAdmissionClass.CHECKPOINT_BARRIER_CONTROL:
                if (
                    self._state
                    not in {
                        CheckpointDispatchGateState.DRAINING,
                        CheckpointDispatchGateState.ACTIVE,
                    }
                    or checkpoint_id is None
                    or checkpoint_id != self._checkpoint_id
                ):
                    raise CheckpointDispatchGateClosed(
                        "checkpoint control write has no exact process-local owner"
                    )
                return None

            if admission_class is LedgerWriteAdmissionClass.RECONCILIATION_CONTROL:
                return None

            raise AssertionError(f"unsupported ledger admission class: {admission_class}")

    def promote_write_admission(
        self,
        token: ProducerAdmissionToken,
        *,
        operation_owner_ids: tuple[str, ...],
    ) -> tuple[ProducerAdmissionToken, ...]:
        """Transfer one admitted writer into exact long-lived operation owners."""

        if not operation_owner_ids or len(operation_owner_ids) != len(
            set(operation_owner_ids)
        ):
            raise CheckpointDispatchGateClosed(
                "operation admission owners must be non-empty and unique"
            )
        with self._condition:
            if self._active_producers.get(token.token_id) != token:
                raise CheckpointDispatchGateClosed(
                    "producer admission promotion lost its writer owner"
                )
            promoted = tuple(
                ProducerAdmissionToken(
                    token_id=f"checkpoint-operation:{uuid4().hex}",
                    generation=token.generation,
                    operation_owner_id=owner_id,
                )
                for owner_id in operation_owner_ids
            )
            self._active_producers.pop(token.token_id)
            self._active_producers.update(
                (item.token_id, item) for item in promoted
            )
            self._condition.notify_all()
            return promoted

    def restore_operation_admission(
        self,
        *,
        operation_owner_id: str,
    ) -> ProducerAdmissionToken:
        """Recreate one process owner from a verified durable reservation."""

        if not operation_owner_id:
            raise CheckpointDispatchGateClosed(
                "restored operation admission requires an owner"
            )
        with self._condition:
            if self._state is not CheckpointDispatchGateState.OPEN:
                raise CheckpointDispatchGateClosed(
                    "durable operation reservation conflicts with checkpoint barrier"
                )
            if any(
                item.operation_owner_id == operation_owner_id
                for item in self._active_producers.values()
            ):
                raise CheckpointDispatchGateClosed(
                    "restored operation admission owner is ambiguous"
                )
            token = ProducerAdmissionToken(
                token_id=f"checkpoint-operation:{uuid4().hex}",
                generation=self._generation,
                operation_owner_id=operation_owner_id,
            )
            self._active_producers[token.token_id] = token
            return token

    def release_write_admission(
        self,
        token: ProducerAdmissionToken | None,
    ) -> None:
        if token is None:
            return
        with self._condition:
            if self._active_producers.get(token.token_id) != token:
                raise CheckpointDispatchGateClosed(
                    "producer admission token identity drifted"
                )
            self._active_producers.pop(token.token_id)
            self._condition.notify_all()

    def begin_checkpoint_drain(
        self,
        *,
        checkpoint_id: str,
        checkpoint_candidate_fingerprint: str,
    ) -> CheckpointDrainToken:
        with self._condition:
            if self._state is not CheckpointDispatchGateState.OPEN:
                raise CheckpointDispatchGateClosed(
                    "checkpoint dispatch gate is already owned"
                )
            self._generation += 1
            self._state = CheckpointDispatchGateState.DRAINING
            self._checkpoint_id = checkpoint_id
            self._checkpoint_candidate_fingerprint = (
                checkpoint_candidate_fingerprint
            )
            self._condition.notify_all()
            return CheckpointDrainToken(
                checkpoint_id=checkpoint_id,
                checkpoint_candidate_fingerprint=checkpoint_candidate_fingerprint,
                generation=self._generation,
            )

    def wait_until_drained(
        self,
        token: CheckpointDrainToken,
        *,
        deadline_monotonic: float,
    ) -> None:
        with self._condition:
            self._require_checkpoint_owner(token)
            while self._active_producers:
                remaining = deadline_monotonic - monotonic()
                if remaining <= 0:
                    raise CheckpointDispatchDrainTimeout(
                        "checkpoint producer drain deadline exceeded"
                    )
                self._condition.wait(timeout=remaining)
                self._require_checkpoint_owner(token)

    def mark_durable_active(
        self,
        token: CheckpointDrainToken,
        barrier: CheckpointDispatchBarrierFact,
    ) -> None:
        with self._condition:
            self._require_checkpoint_owner(token)
            if self._state is not CheckpointDispatchGateState.DRAINING:
                raise CheckpointDispatchGateClosed(
                    "checkpoint barrier activation requires DRAINING"
                )
            if (
                barrier.checkpoint_id != token.checkpoint_id
                or barrier.checkpoint_candidate_fingerprint
                != token.checkpoint_candidate_fingerprint
            ):
                raise CheckpointDispatchGateClosed(
                    "durable checkpoint barrier identity drifted"
                )
            self._state = CheckpointDispatchGateState.ACTIVE
            self._condition.notify_all()

    def abort_before_install(self, token: CheckpointDrainToken) -> None:
        with self._condition:
            self._require_checkpoint_owner(token)
            if self._state is not CheckpointDispatchGateState.DRAINING:
                raise CheckpointDispatchGateClosed(
                    "only a pre-install checkpoint drain can be aborted"
                )
            self._open_locked()

    def release_after_terminal(self, *, checkpoint_id: str) -> None:
        with self._condition:
            if (
                self._state is not CheckpointDispatchGateState.ACTIVE
                or checkpoint_id != self._checkpoint_id
            ):
                raise CheckpointDispatchGateClosed(
                    "checkpoint terminal release lost its exact active owner"
                )
            self._open_locked()

    def latch_reconciliation(
        self,
        *,
        checkpoint_id: str,
        checkpoint_candidate_fingerprint: str,
    ) -> None:
        with self._condition:
            if self._checkpoint_id not in (None, checkpoint_id):
                raise CheckpointDispatchGateClosed(
                    "checkpoint reconciliation owner identity drifted"
                )
            if self._checkpoint_candidate_fingerprint not in (
                None,
                checkpoint_candidate_fingerprint,
            ):
                raise CheckpointDispatchGateClosed(
                    "checkpoint reconciliation candidate identity drifted"
                )
            self._checkpoint_id = checkpoint_id
            self._checkpoint_candidate_fingerprint = (
                checkpoint_candidate_fingerprint
            )
            self._state = CheckpointDispatchGateState.RECONCILIATION_REQUIRED
            self._condition.notify_all()

    def _require_checkpoint_owner(self, token: CheckpointDrainToken) -> None:
        if (
            token.generation != self._generation
            or token.checkpoint_id != self._checkpoint_id
            or token.checkpoint_candidate_fingerprint
            != self._checkpoint_candidate_fingerprint
        ):
            raise CheckpointDispatchGateClosed(
                "checkpoint drain token identity drifted"
            )

    def _open_locked(self) -> None:
        self._generation += 1
        self._state = CheckpointDispatchGateState.OPEN
        self._checkpoint_id = None
        self._checkpoint_candidate_fingerprint = None
        self._condition.notify_all()


__all__ = [
    "CheckpointDispatchBarrierCoordinator",
    "CheckpointDispatchDrainTimeout",
    "CheckpointDispatchGateClosed",
    "CheckpointDispatchGateState",
    "CheckpointDrainToken",
    "ProducerAdmissionToken",
]
