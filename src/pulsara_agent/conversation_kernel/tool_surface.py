"""Process-local exact tool-surface access for one Host activation.

Provider-visible tool facts deliberately stop at the semantic descriptor.  The
objects in this module bind that immutable semantic surface to one exact set of
physical executors without allowing executor identity to leak into model input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable

from pulsara_agent.capability.contracts import (
    FrozenToolCapabilityExposurePlan,
    PreparedUnavailableDirectMcpGate,
)
from pulsara_agent.model_input.contracts import (
    FrozenModelToolSurface,
    ModelInputScopeKind,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin


_MCP_STANDARD_TOOL_NAMES = frozenset(
    {
        "get_mcp_prompt",
        "inspect_new_mcp_tool",
        "list_mcp_prompts",
        "list_mcp_resource_templates",
        "list_mcp_resources",
        "list_mcp_servers",
        "read_mcp_resource",
        "use_new_mcp_tool",
    }
)
_TERMINAL_TOOL_NAMES = frozenset(
    {"terminal", "terminal_process", "terminal_monitor"}
)
_PLAN_CONTROL_TOOL_NAMES = frozenset(
    {"enter_plan", "ask_plan_question", "exit_plan"}
)
_CAPABILITY_DISPATCH_OBSERVATION_SEAL = object()


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

    def __post_init__(self) -> None:
        if not self.tool_name or not self.catalog_entry_fingerprint:
            raise ValueError("builtin execution policy identity is incomplete")


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


ToolExecutionPolicy = BuiltinExecutionPolicyRef | McpToolExecutionPolicyFact


def execution_policy_fingerprint(policy: ToolExecutionPolicy) -> str:
    if isinstance(policy, BuiltinExecutionPolicyRef):
        return context_fingerprint(
            "builtin-execution-policy-ref:v1",
            {
                "tool_name": policy.tool_name,
                "catalog_entry_fingerprint": policy.catalog_entry_fingerprint,
            },
        )
    if isinstance(policy, McpToolExecutionPolicyFact):
        return context_fingerprint(
            "mcp-tool-execution-policy:v1",
            {
                "server_id": policy.server_id,
                "remote_tool_name": policy.remote_tool_name,
                "provider_tool_name": policy.provider_tool_name,
                "tool_semantic_fingerprint": policy.tool_semantic_fingerprint,
                "effect_kind": policy.effect_kind.value,
                "timeout_ms": policy.timeout_ms,
                "parallel_safe": policy.parallel_safe,
                "classification_source": policy.classification_source.value,
            },
        )
    raise TypeError("tool execution policy union is open")


@dataclass(frozen=True, slots=True)
class PreparedToolExecutionBinding:
    tool_name: str
    descriptor_fingerprint: str
    executor_binding_fingerprint: str
    execution_policy: ToolExecutionPolicy
    memory_citation_visibility: str = "CURRENT_CONTEXT_BOUND"
    memory_citation_evidence_kind: str = "PRIMARY_OBSERVATION"

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
        if self.memory_citation_visibility not in {"GLOBAL_SAFE", "CURRENT_CONTEXT_BOUND"}:
            raise ValueError("memory citation visibility is not closed")
        if self.memory_citation_evidence_kind not in {
            "PRIMARY_OBSERVATION",
            "MEMORY_READ_EXPOSURE",
        }:
            raise ValueError("memory citation evidence kind is not closed")


DirectToolAccessLeaf = PreparedToolExecutionBinding | PreparedUnavailableDirectMcpGate


def tool_observation_origin_for_binding(
    binding: PreparedToolExecutionBinding,
) -> ToolObservationOrigin:
    """Freeze observation origin from the exact advertised executor binding."""

    tool_name = binding.tool_name
    if (
        isinstance(binding.execution_policy, McpToolExecutionPolicyFact)
        or tool_name in _MCP_STANDARD_TOOL_NAMES
    ):
        return ToolObservationOrigin.MCP_REMOTE
    if tool_name in _TERMINAL_TOOL_NAMES:
        return ToolObservationOrigin.TERMINAL_PROCESS
    if tool_name in _PLAN_CONTROL_TOOL_NAMES:
        return ToolObservationOrigin.PLAN_CONTROL
    if isinstance(binding.execution_policy, BuiltinExecutionPolicyRef):
        return ToolObservationOrigin.BUILTIN
    raise TypeError("tool execution policy union is open")


@dataclass(frozen=True, slots=True)
class ProcessLocalToolSurfaceAccess:
    owner_epoch: int
    surface_generation: int
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    _authority: object = field(repr=False)

    def exactly_joins(self, other: ProcessLocalToolSurfaceAccess) -> bool:
        return self is other


@dataclass(frozen=True, slots=True)
class PreparedKernelToolSurface:
    model_surface: FrozenModelToolSurface
    execution_bindings: tuple[DirectToolAccessLeaf, ...]
    access: ProcessLocalToolSurfaceAccess = field(repr=False)
    capability_exposure_plan: FrozenToolCapabilityExposurePlan | None = field(
        default=None, repr=False
    )

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
        if self.capability_exposure_plan is not None and (
            self.capability_exposure_plan.direct_tool_surface != self.model_surface
        ):
            raise ValueError("prepared surface does not join capability plan")

    @property
    def executor_binding_fingerprints(self) -> tuple[str, ...]:
        return tuple(
            item.executor_binding_fingerprint for item in self.execution_bindings
        )

    def binding(self, tool_name: str) -> DirectToolAccessLeaf:
        for item in self.execution_bindings:
            if item.tool_name == tool_name:
                return item
        raise KeyError(tool_name)

    def exactly_joins(self, other: PreparedKernelToolSurface) -> bool:
        return self is other


@dataclass(slots=True)
class ProcessLocalToolSurfaceBorrow:
    prepared: PreparedKernelToolSurface
    borrow_id: str
    _authority: object = field(repr=False)
    _validate: Callable[
        ["ProcessLocalToolSurfaceBorrow", str], DirectToolAccessLeaf
    ] = field(repr=False)
    _release: Callable[["ProcessLocalToolSurfaceBorrow"], None] = field(repr=False)
    _closed: bool = False

    def exactly_joins(self, prepared: PreparedKernelToolSurface) -> bool:
        return (
            not self._closed
            and self._authority is prepared.access._authority
            and self.prepared is prepared
        )

    def execution_binding(self, tool_name: str) -> DirectToolAccessLeaf:
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


@dataclass(frozen=True, slots=True, init=False)
class OwnerIssuedCapabilityDispatchObservation:
    """One-shot carrier issued only while ToolRuntime owns the exact borrow."""

    capability_dispatch_cut: object
    prepared_surface: PreparedKernelToolSurface
    surface_borrow: ProcessLocalToolSurfaceBorrow
    _owner: object
    _consumed: bool

    def __init__(
        self,
        *,
        capability_dispatch_cut: object,
        prepared_surface: PreparedKernelToolSurface,
        surface_borrow: ProcessLocalToolSurfaceBorrow,
        owner: object,
        _seal: object,
    ) -> None:
        if _seal is not _CAPABILITY_DISPATCH_OBSERVATION_SEAL:
            raise TypeError("capability dispatch observation is ToolRuntime-issued")
        object.__setattr__(self, "capability_dispatch_cut", capability_dispatch_cut)
        object.__setattr__(self, "prepared_surface", prepared_surface)
        object.__setattr__(self, "surface_borrow", surface_borrow)
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_consumed", False)

    def consume(self) -> tuple[object, ProcessLocalToolSurfaceBorrow]:
        if self._consumed:
            raise RuntimeError("capability dispatch observation is already consumed")
        object.__setattr__(self, "_consumed", True)
        return self.capability_dispatch_cut, self.surface_borrow


def _issue_capability_dispatch_observation(
    *,
    capability_dispatch_cut: object,
    prepared_surface: PreparedKernelToolSurface,
    surface_borrow: ProcessLocalToolSurfaceBorrow,
    owner: object,
) -> OwnerIssuedCapabilityDispatchObservation:
    return OwnerIssuedCapabilityDispatchObservation(
        capability_dispatch_cut=capability_dispatch_cut,
        prepared_surface=prepared_surface,
        surface_borrow=surface_borrow,
        owner=owner,
        _seal=_CAPABILITY_DISPATCH_OBSERVATION_SEAL,
    )


__all__ = [
    "BuiltinExecutionPolicyRef",
    "DirectToolAccessLeaf",
    "McpEffectKind",
    "McpPolicyClassificationSource",
    "McpToolExecutionPolicyFact",
    "OwnerIssuedCapabilityDispatchObservation",
    "PreparedKernelToolSurface",
    "PreparedToolExecutionBinding",
    "ProcessLocalToolSurfaceAccess",
    "ProcessLocalToolSurfaceBorrow",
    "ToolExecutionPolicy",
    "execution_policy_fingerprint",
    "tool_observation_origin_for_binding",
]
