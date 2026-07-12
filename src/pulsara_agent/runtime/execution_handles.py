"""Process-local execution resource handles and generation-aware borrows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from pulsara_agent.capability.runtime import FrozenCapabilityExecutionSurface


class CapabilityExecutionBorrowUnavailable(RuntimeError):
    pass


@dataclass(slots=True)
class CapabilityExecutionBorrowTracker:
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
            self.active_parent_tool_call_borrows == 0
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
class BoundaryExecutionHandles:
    handle_id: str
    handle_generation: int
    owner_id: str
    state: Literal["attempt_owned", "run_owned", "retiring", "closed"]
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

    def transfer_to_run(self, run_id: str) -> None:
        if self.state != "attempt_owned":
            raise RuntimeError("only attempt-owned handles can transfer to a run")
        self.owner_id = run_id
        self.state = "run_owned"
        self.borrow_tracker.set_authority_active(
            handle_id=self.handle_id,
            generation=self.handle_generation,
            active=True,
        )

    def mark_retiring(self) -> None:
        if self.state not in {"attempt_owned", "run_owned"}:
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


__all__ = [
    "BoundaryExecutionHandles",
    "CapabilityExecutionBorrowAuthority",
    "CapabilityExecutionBorrowTracker",
    "CapabilityExecutionBorrowUnavailable",
]
