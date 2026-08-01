"""Process-local execution resource handles and generation-aware borrows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal
from uuid import uuid4

from pulsara_agent.capability.runtime import FrozenCapabilityExecutionSurface
from pulsara_agent.ports.run_execution import (
    PreparedRunOwnerReservationKey,
    RunOwnerIdentity,
)


class CapabilityExecutionBorrowUnavailable(RuntimeError):
    pass


@dataclass(slots=True)
class CapabilityExecutionBorrowTracker:
    active_activation_borrows: int = 0
    active_parent_tool_call_borrows: int = 0
    active_child_tool_call_borrows: int = 0
    on_change: Callable[[], None] | None = field(default=None, repr=False)
    authority_handle_id: str | None = field(default=None, init=False)
    authority_generation: int | None = field(default=None, init=False)
    accepting_new_borrows: bool = field(default=True, init=False)

    def bind_authority(
        self,
        *,
        handle_id: str,
        generation: int,
        active: bool,
    ) -> None:
        if self.authority_handle_id is not None:
            raise RuntimeError("capability borrow tracker is already bound")
        self.authority_handle_id = handle_id
        self.authority_generation = generation
        self.accepting_new_borrows = active

    def set_authority_active(
        self,
        *,
        handle_id: str,
        generation: int,
        active: bool,
    ) -> None:
        if (
            self.authority_handle_id != handle_id
            or self.authority_generation != generation
        ):
            raise RuntimeError("capability borrow authority identity mismatch")
        self.accepting_new_borrows = active

    def can_retire(self) -> bool:
        return (
            self.active_activation_borrows == 0
            and self.active_parent_tool_call_borrows == 0
            and self.active_child_tool_call_borrows == 0
        )

    def _change(self, field_name: str, delta: int) -> None:
        if delta > 0 and not self.accepting_new_borrows:
            raise CapabilityExecutionBorrowUnavailable(
                "execution handles no longer accept new borrows"
            )
        value = getattr(self, field_name) + delta
        if value < 0:
            raise RuntimeError(f"capability borrow underflow: {field_name}")
        setattr(self, field_name, value)
        if self.on_change is not None:
            self.on_change()

    def borrow_parent_tool_call(self) -> None:
        self._change("active_parent_tool_call_borrows", 1)

    def release_parent_tool_call(self) -> None:
        self._change("active_parent_tool_call_borrows", -1)

    def borrow_child_tool_call(self) -> None:
        self._change("active_child_tool_call_borrows", 1)

    def release_child_tool_call(self) -> None:
        self._change("active_child_tool_call_borrows", -1)

    def borrow_activation(self) -> None:
        self._change("active_activation_borrows", 1)

    def release_activation(self) -> None:
        self._change("active_activation_borrows", -1)


@dataclass(frozen=True, slots=True)
class CapabilityExecutionBorrowAuthority:
    handle_id: str
    handle_generation: int
    tracker: CapabilityExecutionBorrowTracker = field(repr=False)

    @property
    def is_active(self) -> bool:
        return (
            self.tracker.authority_handle_id == self.handle_id
            and self.tracker.authority_generation == self.handle_generation
            and self.tracker.accepting_new_borrows
        )

    def _require_identity(self) -> None:
        if (
            self.tracker.authority_handle_id != self.handle_id
            or self.tracker.authority_generation != self.handle_generation
        ):
            raise CapabilityExecutionBorrowUnavailable(
                "execution borrow authority identity is stale"
            )

    def borrow_parent_tool_call(self) -> None:
        self._require_identity()
        self.tracker.borrow_parent_tool_call()

    def release_parent_tool_call(self) -> None:
        self._require_identity()
        self.tracker.release_parent_tool_call()

    def borrow_child_tool_call(self) -> None:
        self._require_identity()
        self.tracker.borrow_child_tool_call()

    def release_child_tool_call(self) -> None:
        self._require_identity()
        self.tracker.release_child_tool_call()


@dataclass(slots=True)
class RunExecutionHandleSet:
    handle_id: str
    handle_generation: int
    owner: PreparedRunOwnerReservationKey | RunOwnerIdentity
    state: Literal["boundary_owned", "run_owned", "retiring", "closed"]
    mcp_installation: Any
    capability_runtime: Any
    tool_registry: Any
    frozen_execution_surface: FrozenCapabilityExecutionSurface
    borrow_tracker: CapabilityExecutionBorrowTracker = field(
        default_factory=CapabilityExecutionBorrowTracker
    )

    def __post_init__(self) -> None:
        self.borrow_tracker.bind_authority(
            handle_id=self.handle_id,
            generation=self.handle_generation,
            active=self.state == "run_owned",
        )

    @property
    def borrow_authority(self) -> CapabilityExecutionBorrowAuthority:
        return CapabilityExecutionBorrowAuthority(
            handle_id=self.handle_id,
            handle_generation=self.handle_generation,
            tracker=self.borrow_tracker,
        )

    def transfer_to_run(self, owner: RunOwnerIdentity) -> None:
        if self.state != "boundary_owned":
            raise RuntimeError("only boundary-owned handles can transfer to a run")
        if (
            self.owner.runtime_session_id != owner.runtime_session_id
            or self.owner.run_id != owner.run_id
            or self.owner.run_start_event_id != owner.run_start_event_id
        ):
            raise RuntimeError("execution handle owner promotion mismatch")
        self.owner = owner
        self.state = "run_owned"
        self.borrow_tracker.set_authority_active(
            handle_id=self.handle_id,
            generation=self.handle_generation,
            active=True,
        )

    def borrow_for_activation(
        self, *, activation_fingerprint: str
    ) -> "RunExecutionHandleBorrow":
        if self.state != "run_owned" or not isinstance(self.owner, RunOwnerIdentity):
            raise CapabilityExecutionBorrowUnavailable(
                "execution handles are not owned by a committed run"
            )
        self.borrow_tracker.borrow_activation()
        authority = _RunExecutionHandleBorrowAuthority(
            source=self,
            activation_fingerprint=activation_fingerprint,
        )
        return RunExecutionHandleBorrow(
            borrow_id=f"run_execution_borrow:{uuid4().hex}",
            source_handle_id=self.handle_id,
            source_handle_generation=self.handle_generation,
            activation_fingerprint=activation_fingerprint,
            state="active",
            _authority=authority,
        )

    def mark_retiring(self) -> None:
        if self.state not in {"boundary_owned", "run_owned"}:
            raise RuntimeError("execution handles cannot re-enter retiring state")
        self.borrow_tracker.set_authority_active(
            handle_id=self.handle_id,
            generation=self.handle_generation,
            active=False,
        )
        self.state = "retiring"

    def mark_closed(self) -> None:
        if self.state != "retiring" or not self.borrow_tracker.can_retire():
            raise RuntimeError("execution handles cannot close with live borrows")
        self.state = "closed"


@dataclass(slots=True)
class _RunExecutionHandleBorrowAuthority:
    source: RunExecutionHandleSet
    activation_fingerprint: str
    released: bool = False

    def validate_exact(self, borrow: "RunExecutionHandleBorrow") -> None:
        if self.released or borrow.state != "active":
            raise CapabilityExecutionBorrowUnavailable("activation borrow is released")
        if (
            self.source.handle_id != borrow.source_handle_id
            or self.source.handle_generation != borrow.source_handle_generation
            or self.activation_fingerprint != borrow.activation_fingerprint
        ):
            raise CapabilityExecutionBorrowUnavailable("activation borrow is stale")

    def release(self, borrow: "RunExecutionHandleBorrow") -> None:
        if self.released:
            return
        self.validate_exact(borrow)
        self.released = True
        borrow.state = "released"
        self.source.borrow_tracker.release_activation()


@dataclass(slots=True)
class RunExecutionHandleBorrow:
    borrow_id: str
    source_handle_id: str
    source_handle_generation: int
    activation_fingerprint: str
    state: Literal["active", "released"]
    _authority: _RunExecutionHandleBorrowAuthority = field(repr=False)

    def validate_exact(self) -> None:
        self._authority.validate_exact(self)

    def release(self) -> None:
        self._authority.release(self)


__all__ = [
    "RunExecutionHandleSet",
    "RunExecutionHandleBorrow",
    "CapabilityExecutionBorrowAuthority",
    "CapabilityExecutionBorrowTracker",
    "CapabilityExecutionBorrowUnavailable",
]
