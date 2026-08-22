"""Provider-neutral process-local Tool execution contracts.

The module contains values shared by the foreground coordinator and concrete
Tool owners.  It owns no repository, provider adapter, Host, or executor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping, Protocol

from pulsara_agent.conversation_kernel.capability_composition import (
    PreparedMcpCapabilitySourceSnapshotSet,
    SealedBuiltinCapabilitySnapshot,
)
from pulsara_agent.conversation_kernel.compaction.runtime_handoff import (
    FrozenCompactionRuntimeHandoff,
)
from pulsara_agent.conversation_kernel.memory.contracts import (
    FrozenModelCallMemoryContext,
    FrozenModelVisibleMemoryProvenance,
    ModelVisibleMemoryProvenanceDisposition,
    PreparedMemoryCandidateAcceptance,
)
from pulsara_agent.conversation_kernel.tool_surface import (
    PreparedKernelToolSurface,
    ProcessLocalToolSurfaceBorrow,
)
from pulsara_agent.capability.contracts import (
    FrozenMcpCapabilityProjectionInput,
    FrozenToolCapabilityExposurePlan,
)
from pulsara_agent.model_input.contracts import (
    FrozenCanonicalCompileSnapshot,
    FrozenCompiledModelInput,
    ModelInputScopeKind,
)
from pulsara_agent.model_input.continuity import (
    ProcessLocalProviderInputInstallPermit,
)
from pulsara_agent.conversation_kernel.subagents.contracts import (
    FrozenSubagentParentContextCallSubject,
)
from pulsara_agent.ports.tool_execution import (
    ToolOutputArtifactCandidate,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot
from pulsara_agent.primitives.tool_observation import (
    PhysicalToolObservationSupplement,
    TrustedToolObservationSupplement,
)


@dataclass(frozen=True, slots=True)
class KernelToolResult:
    state: str
    content: bytes
    memory_candidate: PreparedMemoryCandidateAcceptance | None = None
    remote_identity: str | None = None
    output_artifact_candidate: ToolOutputArtifactCandidate | None = None
    artifact_source_read: bool = False
    process_local_settlement: ProcessLocalEffectSettlementToken | None = None
    physical_timing: str = "ON_TIME"
    caller_cancelled_while_running: bool = False
    effect_class: str | None = None
    physical_observation: PhysicalToolObservationSupplement | None = None
    trusted_observation: TrustedToolObservationSupplement | None = None
    model_visible_memory_fact_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.content.decode("utf-8")
        if self.artifact_source_read and self.output_artifact_candidate is not None:
            raise ValueError("artifact_read cannot recursively own an artifact")


class KernelToolPhysicalInvocationError(RuntimeError):
    """Process-local exact exception classified by the frozen Tool contract."""

    def __init__(
        self,
        *,
        effect_class: str,
        error: BaseException,
        timing: str,
        caller_cancelled: bool,
        physical_observation: PhysicalToolObservationSupplement | None = None,
    ) -> None:
        self.effect_class = effect_class
        self.physical_error = error
        self.timing = timing
        self.caller_cancelled = caller_cancelled
        self.physical_observation = physical_observation
        super().__init__(f"tool physical invocation raised: {type(error).__name__}")


@dataclass(frozen=True, slots=True)
class KernelToolInvocationContext:
    session_id: str
    workspace_id: str
    turn_id: str
    assistant_entry_id: str
    tool_call_id: str
    attempt_id: str
    result_entry_id: str
    conversation_scope_kind: str
    scope_subagent_task_id: str | None
    host_owner_epoch: int
    authorization_reference: str
    permission_snapshot_fingerprint: str
    effective_permission_mode: PermissionMode
    attempt_permission_snapshot_fingerprint: str
    surface_borrow: ProcessLocalToolSurfaceBorrow = field(repr=False, compare=False)
    subagent_parent_context_subject: FrozenSubagentParentContextCallSubject | None = (
        field(default=None, repr=False)
    )
    memory_context: FrozenModelCallMemoryContext = field(
        default_factory=lambda: FrozenModelCallMemoryContext(
            FrozenModelVisibleMemoryProvenance(
                ModelVisibleMemoryProvenanceDisposition.COMPLETE,
                (),
            )
        ),
        repr=False,
    )

    def __post_init__(self) -> None:
        if not all(
            (
                self.session_id,
                self.workspace_id,
                self.turn_id,
                self.assistant_entry_id,
                self.tool_call_id,
                self.attempt_id,
                self.result_entry_id,
                self.authorization_reference,
                self.permission_snapshot_fingerprint,
                self.attempt_permission_snapshot_fingerprint,
            )
        ):
            raise ValueError("kernel tool invocation context is incomplete")
        if self.conversation_scope_kind not in {"ROOT", "SUBAGENT_TASK"}:
            raise ValueError("kernel tool invocation scope is invalid")
        if (self.conversation_scope_kind == "ROOT") != (
            self.scope_subagent_task_id is None
        ):
            raise ValueError("kernel tool invocation scope identity is invalid")
        if (
            self.attempt_permission_snapshot_fingerprint
            != self.permission_snapshot_fingerprint
        ):
            raise ValueError(
                "tool attempt permission snapshot does not exact-join the run"
            )
        if not isinstance(self.effective_permission_mode, PermissionMode):
            raise TypeError("tool invocation permission mode must be closed")
        access = self.surface_borrow.prepared.access
        if (
            access.conversation_scope_kind.value != self.conversation_scope_kind
            or access.scope_subagent_task_id != self.scope_subagent_task_id
        ):
            raise ValueError("kernel tool invocation scope access does not exact-join")
        if (
            self.conversation_scope_kind == "SUBAGENT_TASK"
            and self.subagent_parent_context_subject is not None
        ):
            raise ValueError("kernel invocation parent-call subject union is invalid")
        if self.subagent_parent_context_subject is not None and (
            self.subagent_parent_context_subject.session_id != self.session_id
            or self.subagent_parent_context_subject.caller_turn_id != self.turn_id
        ):
            raise ValueError("kernel invocation parent-call subject identity drifted")


@dataclass(frozen=True, slots=True)
class ProcessLocalEffectSettlementToken:
    token_id: str
    prepared: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.token_id:
            raise ValueError("process-local settlement token is invalid")


class ProcessLocalEffectSettlementDisposition(StrEnum):
    COMMITTED = "COMMITTED"
    DISCARDED = "DISCARDED"


class ProcessLocalEffectSettlementOutcome(StrEnum):
    INSTALLED = "INSTALLED"
    DISCARDED = "DISCARDED"


@dataclass(frozen=True, slots=True)
class ProcessLocalEffectSettlementResult:
    outcome: ProcessLocalEffectSettlementOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ProcessLocalEffectSettlementOutcome):
            raise TypeError("process-local settlement outcome must be closed")


class KernelToolLiveSink(Protocol):
    def offer_text(self, text: str) -> None: ...


class KernelToolAuthorizationKind(StrEnum):
    ALLOW = "ALLOW"
    REQUIRE_CONFIRMATION = "REQUIRE_CONFIRMATION"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
    CANCELLED_BEFORE_DISPATCH = "CANCELLED_BEFORE_DISPATCH"


@dataclass(frozen=True, slots=True)
class KernelToolAuthorization:
    kind: KernelToolAuthorizationKind
    reference: str
    public_message: str = ""
    accepted_attempt_id: str | None = None
    accepted_result_entry_id: str | None = None
    accepted_permission_snapshot_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if (
            self.accepted_attempt_id is not None
            and self.kind is not KernelToolAuthorizationKind.ALLOW
        ):
            raise ValueError("only an allowed authorization may own an attempt")
        if (
            self.accepted_result_entry_id is not None
            and self.kind is not KernelToolAuthorizationKind.PERMISSION_DENIED
        ):
            raise ValueError("only a denied authorization may own a result")
        if (
            self.accepted_attempt_id is not None
            and self.accepted_result_entry_id is not None
        ):
            raise ValueError("authorization effect union is invalid")
        if (self.accepted_attempt_id is not None) != (
            self.accepted_permission_snapshot_fingerprint is not None
        ):
            raise ValueError("accepted attempt permission attribution is incomplete")


class ToolSurfacePlanningPort(Protocol):
    def sealed_builtin_capability_snapshot(
        self,
        *,
        conversation_scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
    ) -> SealedBuiltinCapabilitySnapshot: ...

    def freeze_mcp_capability_source_snapshot_set(
        self,
        *,
        conversation_scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
    ) -> PreparedMcpCapabilitySourceSnapshotSet: ...

    def freeze_mcp_capability_projection_input(
        self, owner: PreparedMcpCapabilitySourceSnapshotSet
    ) -> FrozenMcpCapabilityProjectionInput: ...

    def prepare_planned_tool_surface(
        self,
        *,
        plan: FrozenToolCapabilityExposurePlan,
        builtin: SealedBuiltinCapabilitySnapshot,
    ) -> PreparedKernelToolSurface: ...

    def borrow_tool_surface(
        self, prepared: PreparedKernelToolSurface
    ) -> ProcessLocalToolSurfaceBorrow: ...

    def validate_tool_surface_borrow(
        self,
        borrow: ProcessLocalToolSurfaceBorrow,
        prepared: PreparedKernelToolSurface,
    ) -> None: ...

    def install_provider_input_tool_result_deliveries(
        self,
        *,
        permit: ProcessLocalProviderInputInstallPermit,
        canonical_facts: FrozenCanonicalCompileSnapshot,
        compiled_input: FrozenCompiledModelInput,
        surface_borrow: ProcessLocalToolSurfaceBorrow,
    ) -> None: ...

    async def freeze_compaction_runtime_handoff(
        self,
        *,
        conversation_scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        maximum_utf8_bytes: int,
    ) -> FrozenCompactionRuntimeHandoff | None: ...


class ToolInvocationPort(Protocol):
    async def authorize(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        tool_call_id: str,
        turn_id: str,
        assistant_entry_id: str,
        permission_snapshot: FrozenRunPermissionSnapshot,
        surface_borrow: ProcessLocalToolSurfaceBorrow,
        memory_context: FrozenModelCallMemoryContext,
    ) -> KernelToolAuthorization: ...

    async def request_confirmation(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
        turn_id: str,
        assistant_entry_id: str,
        permission_snapshot: FrozenRunPermissionSnapshot,
    ) -> KernelToolAuthorization: ...

    async def invoke(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        tool_call_id: str,
        attempt_id: str,
        turn_id: str,
        assistant_entry_id: str,
        invocation_context: KernelToolInvocationContext,
        live_sink: KernelToolLiveSink | None = None,
    ) -> KernelToolResult: ...

    async def settle_process_local_effect(
        self,
        token: ProcessLocalEffectSettlementToken,
        disposition: ProcessLocalEffectSettlementDisposition,
    ) -> ProcessLocalEffectSettlementResult: ...


__all__ = [
    "KernelToolAuthorization",
    "KernelToolAuthorizationKind",
    "KernelToolInvocationContext",
    "KernelToolLiveSink",
    "KernelToolPhysicalInvocationError",
    "KernelToolResult",
    "ProcessLocalEffectSettlementDisposition",
    "ProcessLocalEffectSettlementOutcome",
    "ProcessLocalEffectSettlementResult",
    "ProcessLocalEffectSettlementToken",
    "ToolInvocationPort",
    "ToolSurfacePlanningPort",
]
