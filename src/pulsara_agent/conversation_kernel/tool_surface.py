"""Process-local exact tool-surface access for one Host activation.

Provider-visible tool facts deliberately stop at the semantic descriptor.  The
objects in this module bind that immutable semantic surface to one exact set of
physical executors without allowing executor identity to leak into model input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable

from pulsara_agent.model_input.contracts import (
    FrozenModelToolSurface,
    ModelInputScopeKind,
)
from pulsara_agent.primitives.context import context_fingerprint


class McpEffectKind(StrEnum):
    READ_ONLY = "READ_ONLY"
    EXTERNAL_EFFECT = "EXTERNAL_EFFECT"


class McpPolicyClassificationSource(StrEnum):
    TOOL_OVERRIDE = "TOOL_OVERRIDE"
    SERVER_OVERRIDE = "SERVER_OVERRIDE"
    SERVER_ANNOTATIONS = "SERVER_ANNOTATIONS"


@dataclass(frozen=True, slots=True)
class BuiltinExecutionPolicyRef:
    """Closed reference to the existing builtin catalog policy."""

    tool_name: str
    catalog_entry_fingerprint: str
    policy_fingerprint: str

    def __post_init__(self) -> None:
        if not self.tool_name or not self.catalog_entry_fingerprint:
            raise ValueError("builtin execution policy identity is incomplete")
        expected = context_fingerprint(
            "builtin-execution-policy-ref:v1",
            {
                "tool_name": self.tool_name,
                "catalog_entry_fingerprint": self.catalog_entry_fingerprint,
            },
        )
        if self.policy_fingerprint != expected:
            raise ValueError("builtin execution policy fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class McpToolExecutionPolicyFact:
    """Minimal dynamic MCP policy frozen with one discovery generation."""

    server_id: str
    remote_tool_name: str
    provider_tool_name: str
    tool_semantic_fingerprint: str
    effect_kind: McpEffectKind
    timeout_ms: int
    parallel_safe: bool
    classification_source: McpPolicyClassificationSource
    policy_fingerprint: str

    def __post_init__(self) -> None:
        if not all(
            (
                self.server_id,
                self.remote_tool_name,
                self.provider_tool_name,
                self.tool_semantic_fingerprint,
            )
        ):
            raise ValueError("MCP execution policy identity is incomplete")
        if not 1_000 <= self.timeout_ms <= 600_000:
            raise ValueError("MCP execution timeout is out of range")
        expected = context_fingerprint(
            "mcp-tool-execution-policy:v1",
            {
                "server_id": self.server_id,
                "remote_tool_name": self.remote_tool_name,
                "provider_tool_name": self.provider_tool_name,
                "tool_semantic_fingerprint": self.tool_semantic_fingerprint,
                "effect_kind": self.effect_kind.value,
                "timeout_ms": self.timeout_ms,
                "parallel_safe": self.parallel_safe,
                "classification_source": self.classification_source.value,
            },
        )
        if self.policy_fingerprint != expected:
            raise ValueError("MCP execution policy fingerprint mismatch")


ToolExecutionPolicy = BuiltinExecutionPolicyRef | McpToolExecutionPolicyFact


def execution_policy_fingerprint(policy: ToolExecutionPolicy) -> str:
    if isinstance(policy, (BuiltinExecutionPolicyRef, McpToolExecutionPolicyFact)):
        return policy.policy_fingerprint
    raise TypeError("tool execution policy union is open")


@dataclass(frozen=True, slots=True)
class PreparedToolExecutionBinding:
    tool_name: str
    descriptor_fingerprint: str
    executor_binding_fingerprint: str
    execution_policy: ToolExecutionPolicy

    def __post_init__(self) -> None:
        if not all(
            (
                self.tool_name,
                self.descriptor_fingerprint,
                self.executor_binding_fingerprint,
            )
        ):
            raise ValueError("prepared tool execution binding is incomplete")
        if isinstance(self.execution_policy, BuiltinExecutionPolicyRef):
            if self.execution_policy.tool_name != self.tool_name:
                raise ValueError("builtin policy does not join tool binding")
        elif isinstance(self.execution_policy, McpToolExecutionPolicyFact):
            if (
                self.execution_policy.provider_tool_name != self.tool_name
                or self.execution_policy.tool_semantic_fingerprint
                != self.descriptor_fingerprint
            ):
                raise ValueError("MCP policy does not join tool binding")
        else:
            raise TypeError("tool execution policy union is open")


def tool_execution_surface_fingerprint(
    *,
    owner_epoch: int,
    surface_generation: int,
    semantic_surface_fingerprint: str,
    bindings: tuple[PreparedToolExecutionBinding, ...],
) -> str:
    return context_fingerprint(
        "kernel-tool-execution-surface:v1",
        {
            "owner_epoch": owner_epoch,
            "surface_generation": surface_generation,
            "semantic_surface_fingerprint": semantic_surface_fingerprint,
            "bindings": tuple(
                {
                    "tool_name": item.tool_name,
                    "descriptor_fingerprint": item.descriptor_fingerprint,
                    "executor_binding_fingerprint": (
                        item.executor_binding_fingerprint
                    ),
                    "execution_policy_fingerprint": execution_policy_fingerprint(
                        item.execution_policy
                    ),
                }
                for item in bindings
            ),
        },
    )


@dataclass(frozen=True, slots=True)
class ProcessLocalToolSurfaceAccess:
    owner_epoch: int
    surface_generation: int
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    semantic_surface_fingerprint: str
    execution_surface_fingerprint: str
    _authority: object = field(repr=False)

    @property
    def surface_fingerprint(self) -> str:
        """Compatibility spelling for the provider-semantic fingerprint."""

        return self.semantic_surface_fingerprint

    def exactly_joins(self, other: ProcessLocalToolSurfaceAccess) -> bool:
        return (
            self.owner_epoch == other.owner_epoch
            and self.surface_generation == other.surface_generation
            and self.conversation_scope_kind is other.conversation_scope_kind
            and self.scope_subagent_task_id == other.scope_subagent_task_id
            and self.semantic_surface_fingerprint
            == other.semantic_surface_fingerprint
            and self.execution_surface_fingerprint
            == other.execution_surface_fingerprint
            and self._authority is other._authority
        )


@dataclass(frozen=True, slots=True)
class PreparedKernelToolSurface:
    model_surface: FrozenModelToolSurface
    execution_bindings: tuple[PreparedToolExecutionBinding, ...]
    execution_surface_fingerprint: str
    access: ProcessLocalToolSurfaceAccess = field(repr=False)

    def __post_init__(self) -> None:
        if (
            self.access.conversation_scope_kind
            is not self.model_surface.conversation_scope_kind
            or (self.access.conversation_scope_kind is ModelInputScopeKind.ROOT)
            != (self.access.scope_subagent_task_id is None)
        ):
            raise ValueError("prepared surface access scope does not exact-join")
        names = tuple(item.tool_name for item in self.execution_bindings)
        semantic_names = tuple(item.name for item in self.model_surface.tool_specs)
        if names != semantic_names:
            raise ValueError("prepared surface binding names do not join specs")
        for semantic, binding in zip(
            self.model_surface.tool_specs, self.execution_bindings, strict=True
        ):
            if semantic.descriptor_fingerprint != binding.descriptor_fingerprint:
                raise ValueError("prepared surface descriptor does not exact-join")
        expected = tool_execution_surface_fingerprint(
            owner_epoch=self.access.owner_epoch,
            surface_generation=self.access.surface_generation,
            semantic_surface_fingerprint=self.model_surface.surface_fingerprint,
            bindings=self.execution_bindings,
        )
        if self.execution_surface_fingerprint != expected:
            raise ValueError("prepared execution surface fingerprint mismatch")
        if (
            self.access.semantic_surface_fingerprint
            != self.model_surface.surface_fingerprint
            or self.access.execution_surface_fingerprint
            != self.execution_surface_fingerprint
        ):
            raise ValueError("prepared surface access fingerprint mismatch")

    @property
    def executor_binding_fingerprints(self) -> tuple[str, ...]:
        return tuple(
            item.executor_binding_fingerprint for item in self.execution_bindings
        )

    def binding(self, tool_name: str) -> PreparedToolExecutionBinding:
        for item in self.execution_bindings:
            if item.tool_name == tool_name:
                return item
        raise KeyError(tool_name)

    def exactly_joins(self, other: PreparedKernelToolSurface) -> bool:
        return (
            self.model_surface == other.model_surface
            and self.execution_bindings == other.execution_bindings
            and self.execution_surface_fingerprint
            == other.execution_surface_fingerprint
            and self.access.exactly_joins(other.access)
        )


@dataclass(slots=True)
class ProcessLocalToolSurfaceBorrow:
    prepared: PreparedKernelToolSurface
    borrow_id: str
    _authority: object = field(repr=False)
    _validate: Callable[
        ["ProcessLocalToolSurfaceBorrow", str], PreparedToolExecutionBinding
    ] = field(repr=False)
    _release: Callable[["ProcessLocalToolSurfaceBorrow"], None] = field(repr=False)
    _closed: bool = False

    def exactly_joins(self, prepared: PreparedKernelToolSurface) -> bool:
        return (
            not self._closed
            and self._authority is prepared.access._authority
            and self.prepared.exactly_joins(prepared)
        )

    def execution_binding(self, tool_name: str) -> PreparedToolExecutionBinding:
        if self._closed:
            raise RuntimeError("tool surface borrow is closed")
        return self._validate(self, tool_name)

    def binding_fingerprint(self, tool_name: str) -> str:
        return self.execution_binding(tool_name).executor_binding_fingerprint

    def close(self) -> None:
        if self._closed:
            return
        self._release(self)
        self._closed = True


__all__ = [
    "BuiltinExecutionPolicyRef",
    "McpEffectKind",
    "McpPolicyClassificationSource",
    "McpToolExecutionPolicyFact",
    "PreparedKernelToolSurface",
    "PreparedToolExecutionBinding",
    "ProcessLocalToolSurfaceAccess",
    "ProcessLocalToolSurfaceBorrow",
    "ToolExecutionPolicy",
    "execution_policy_fingerprint",
    "tool_execution_surface_fingerprint",
]
