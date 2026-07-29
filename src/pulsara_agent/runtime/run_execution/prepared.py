"""Prepared activation ownership before a durable RunOwner exists."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from pulsara_agent.runtime.state import RunActivationWorkingState


@dataclass(slots=True)
class RunActivationStateCarrier:
    """Revocable process-local ownership of one activation working cache.

    The cache is deliberately not part of ``RunOwner`` authority.  A transfer
    changes the sole token that may borrow it; stale boundary, activation, or
    suspension objects therefore cannot continue mutating the cache.
    """

    run_id: str
    generation: int
    owner_token: str
    _working_state: RunActivationWorkingState = field(repr=False)
    _retired: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("activation-state carrier generation must be positive")
        if self._working_state.run_id != self.run_id:
            raise ValueError("activation-state carrier run identity mismatch")
        if not self.owner_token:
            raise ValueError("activation-state carrier owner token is required")

    def borrow(self, *, owner_token: str) -> RunActivationWorkingState:
        if self._retired or owner_token != self.owner_token:
            raise RuntimeError("activation-state borrow authority is unavailable")
        return self._working_state

    def transfer(self, *, expected_owner_token: str, new_owner_token: str) -> None:
        if self._retired or expected_owner_token != self.owner_token:
            raise RuntimeError("activation-state transfer CAS mismatch")
        if not new_owner_token:
            raise ValueError("activation-state transfer requires a new owner token")
        self.owner_token = new_owner_token
        self.generation += 1

    def retire(self, *, owner_token: str) -> None:
        if self._retired:
            return
        if owner_token != self.owner_token:
            raise RuntimeError("activation-state retirement authority mismatch")
        self._retired = True


@dataclass(slots=True)
class PreparedRunActivationOwner:
    """Own one working state until the registry promotes its RunStart.

    The state may be borrowed only by the boundary task that created the
    owner.  Promotion and release revoke that borrow surface immediately, so
    there is never a Host-level second reference that can outlive the boundary.
    """

    run_id: str
    boundary_id: str
    owner_task: asyncio.Task[object]
    generation: int
    _working_state: RunActivationWorkingState = field(repr=False)
    state: Literal["prepared", "promoted", "released"] = "prepared"
    _state_carrier: RunActivationStateCarrier = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("prepared activation generation must be positive")
        if self._working_state.run_id != self.run_id:
            raise ValueError("prepared activation run identity mismatch")
        self._state_carrier = RunActivationStateCarrier(
            run_id=self.run_id,
            generation=self.generation,
            owner_token=self.owner_token,
            _working_state=self._working_state,
        )

    @property
    def owner_token(self) -> str:
        return f"prepared:{self.boundary_id}:{self.generation}"

    @property
    def session_id(self) -> str:
        return self._working_state.session_id

    @property
    def turn_id(self) -> str:
        return self._working_state.turn_id

    @property
    def reply_id(self) -> str:
        return self._working_state.reply_id

    def borrow_for_boundary(
        self, owner_task: asyncio.Task[object] | None = None
    ) -> RunActivationWorkingState:
        current = owner_task or asyncio.current_task()
        if self.state != "prepared" or current is not self.owner_task:
            raise RuntimeError("prepared activation borrow authority is unavailable")
        return self._state_carrier.borrow(owner_token=self.owner_token)

    def peek_for_registry(self, *, boundary_id: str) -> RunActivationWorkingState:
        if self.state != "prepared" or boundary_id != self.boundary_id:
            raise RuntimeError("prepared activation registry authority mismatch")
        return self._state_carrier.borrow(owner_token=self.owner_token)

    def confirm_promoted(
        self,
        *,
        boundary_id: str,
        run_owner_token: str,
    ) -> RunActivationStateCarrier:
        self.peek_for_registry(boundary_id=boundary_id)
        self._state_carrier.transfer(
            expected_owner_token=self.owner_token,
            new_owner_token=run_owner_token,
        )
        self.state = "promoted"
        return self._state_carrier

    def release(self) -> None:
        if self.state == "promoted":
            raise RuntimeError("promoted activation is owned by RunOwner")
        if self.state == "prepared":
            self._state_carrier.retire(owner_token=self.owner_token)
        self.state = "released"

    def request_stop(self, reason: object) -> None:
        if self.state != "prepared":
            raise RuntimeError("prepared activation no longer accepts stop intent")
        from pulsara_agent.runtime.recovery import AbortKind, StopRequest

        if not isinstance(reason, AbortKind):
            raise TypeError("prepared activation stop reason is not typed")
        self._state_carrier.borrow(
            owner_token=self.owner_token
        ).stop_request = StopRequest(reason=reason)

    def transfer_owner_task(
        self,
        *,
        expected: asyncio.Task[object],
        incoming: asyncio.Task[object],
    ) -> None:
        if self.state != "prepared" or self.owner_task is not expected:
            raise RuntimeError("prepared activation task transfer CAS mismatch")
        self.owner_task = incoming


__all__ = ["PreparedRunActivationOwner", "RunActivationStateCarrier"]
