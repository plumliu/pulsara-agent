"""Process-local exact tool-surface access for one Host activation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from pulsara_agent.model_input.contracts import (
    FrozenModelToolSurface,
    ModelInputScopeKind,
)


@dataclass(frozen=True, slots=True)
class ProcessLocalToolSurfaceAccess:
    owner_epoch: int
    surface_generation: int
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    surface_fingerprint: str
    _authority: object = field(repr=False)

    def exactly_joins(self, other: ProcessLocalToolSurfaceAccess) -> bool:
        """Join public surface facts and the opaque Host owner by identity."""

        return (
            self.owner_epoch == other.owner_epoch
            and self.surface_generation == other.surface_generation
            and self.conversation_scope_kind is other.conversation_scope_kind
            and self.scope_subagent_task_id == other.scope_subagent_task_id
            and self.surface_fingerprint == other.surface_fingerprint
            and self._authority is other._authority
        )


@dataclass(frozen=True, slots=True)
class PreparedKernelToolSurface:
    model_surface: FrozenModelToolSurface
    executor_binding_fingerprints: tuple[str, ...]
    access: ProcessLocalToolSurfaceAccess = field(repr=False)

    def __post_init__(self) -> None:
        if (
            self.access.conversation_scope_kind
            is not self.model_surface.conversation_scope_kind
            or (self.access.conversation_scope_kind is ModelInputScopeKind.ROOT)
            != (self.access.scope_subagent_task_id is None)
        ):
            raise ValueError("prepared surface access scope does not exact-join")
        if len(self.executor_binding_fingerprints) != len(
            self.model_surface.tool_specs
        ):
            raise ValueError("prepared surface binding cardinality mismatch")
        if self.executor_binding_fingerprints != tuple(
            item.executor_binding_fingerprint for item in self.model_surface.tool_specs
        ):
            raise ValueError("prepared surface binding identities do not join specs")
        if self.access.surface_fingerprint != self.model_surface.surface_fingerprint:
            raise ValueError("prepared surface access fingerprint mismatch")

    def exactly_joins(self, other: PreparedKernelToolSurface) -> bool:
        """Return whether both carriers name one process-local surface owner."""

        return (
            self.model_surface == other.model_surface
            and self.executor_binding_fingerprints
            == other.executor_binding_fingerprints
            and self.access.exactly_joins(other.access)
        )


@dataclass(slots=True)
class ProcessLocalToolSurfaceBorrow:
    prepared: PreparedKernelToolSurface
    borrow_id: str
    _authority: object = field(repr=False)
    _validate: Callable[["ProcessLocalToolSurfaceBorrow", str], str] = field(repr=False)
    _release: Callable[["ProcessLocalToolSurfaceBorrow"], None] = field(repr=False)
    _closed: bool = False

    def exactly_joins(self, prepared: PreparedKernelToolSurface) -> bool:
        """Bind this active borrow to the surface and opaque Host authority."""

        return (
            not self._closed
            and self._authority is prepared.access._authority
            and self.prepared.exactly_joins(prepared)
        )

    def binding_fingerprint(self, tool_name: str) -> str:
        if self._closed:
            raise RuntimeError("tool surface borrow is closed")
        return self._validate(self, tool_name)

    def close(self) -> None:
        if self._closed:
            return
        self._release(self)
        self._closed = True


__all__ = [
    "PreparedKernelToolSurface",
    "ProcessLocalToolSurfaceAccess",
    "ProcessLocalToolSurfaceBorrow",
]
