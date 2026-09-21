"""Provider-neutral process-local Tool execution contracts.

The module contains values shared by the foreground coordinator and concrete
Tool owners.  It owns no repository, provider adapter, Host, or executor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Mapping, Protocol, TYPE_CHECKING

from pulsara_agent.llm.input import (
    FrozenPromptContent,
    LLMImagePart,
    LLMTextPart,
    frozen_tool_result_public_text,
)

if TYPE_CHECKING:
    from .capability_management_execution import CapabilityManagementCall

from pulsara_agent.conversation_kernel.capability_composition import (
    PreparedMcpCapabilitySourceSnapshotSet,
    SealedBuiltinCapabilitySnapshot,
)
from pulsara_agent.conversation_kernel.compaction.runtime_handoff import (
    FrozenCompactionRuntimeHandoff,
)
from pulsara_agent.conversation_kernel.memory.contracts import (
    FrozenModelCallMemoryContext,
)
from pulsara_agent.conversation_kernel.memory.writes import PreparedMemoryMutation
from pulsara_agent.conversation_kernel.tool_surface import (
    PreparedKernelToolSurface,
    ProcessLocalToolSurfaceBorrow,
)
from pulsara_agent.capability.contracts import (
    FrozenMcpCapabilityProjectionInput,
    FrozenCapabilityDispatchCut,
    FrozenToolCapabilityExposurePlan,
)
from pulsara_agent.model_input.contracts import (
    FrozenCanonicalCompileSnapshot,
    FrozenCompiledModelInput,
    ModelInputScopeKind,
    ProviderToolResultContextMetadata,
)
from pulsara_agent.model_input.continuity import (
    ProcessLocalProviderInputInstallPermit,
)
from pulsara_agent.conversation_kernel.subagents.contracts import (
    FrozenSubagentParentContextCallSubject,
)
from pulsara_agent.ports.tool_execution import (
    ToolOutputArtifactCandidate,
    ToolOutputSourceCoverage,
    ToolOutputSourceCoverageReason,
)
from pulsara_agent.ports.artifact import (
    ToolOutputArtifactDisposition,
    ToolOutputArtifactUnavailabilityReason,
    ToolResultDisplayKind,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.context import FrozenJsonObjectFact
from pulsara_agent.primitives.tool_result_projection import (
    FrozenToolResultDeliveryRequirement,
    classify_tool_result_delivery,
)
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot
from pulsara_agent.primitives.tool_observation import (
    PhysicalToolObservationSupplement,
    ToolObservationOrigin,
    TrustedToolObservationSupplement,
    freeze_tool_observation_timing_fact,
)
from pulsara_agent.tools.builtins.filesystem import ViewImageSource
from pulsara_agent.conversation_kernel.visualization import VisualizationSource


@dataclass(frozen=True, slots=True)
class KernelToolResult:
    state: str
    content: bytes | FrozenPromptContent
    memory_mutation: PreparedMemoryMutation | None = None
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
        if isinstance(self.content, bytes):
            self.content.decode("utf-8")
        elif isinstance(self.content, FrozenPromptContent):
            images = tuple(
                part for part in self.content.parts if isinstance(part, LLMImagePart)
            )
            if (
                self.state != "SUCCESS"
                or len(images) != 1
                or not any(isinstance(part, LLMTextPart) for part in self.content.parts)
            ):
                raise ValueError("typed image content is invalid for this Tool result")
        else:
            raise TypeError("kernel Tool result content must be bytes or frozen content")
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
    input_modalities: tuple[str, ...] | None = None
    image_resource_allowance: "FrozenImageToolResourceAllowance | None" = None
    permission_confirmation_granted: bool = False
    capability_call: CapabilityManagementCall | None = field(default=None, repr=False, compare=False)
    subagent_parent_context_subject: FrozenSubagentParentContextCallSubject | None = (
        field(default=None, repr=False)
    )
    memory_context: FrozenModelCallMemoryContext = field(
        default_factory=FrozenModelCallMemoryContext, repr=False
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
        if not isinstance(self.permission_confirmation_granted, bool):
            raise TypeError("kernel tool permission confirmation must be frozen")
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
        if self.input_modalities is not None and (
            not isinstance(self.input_modalities, tuple)
            or any(not isinstance(value, str) for value in self.input_modalities)
        ):
            raise TypeError("tool invocation input modalities are invalid")
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


@dataclass(frozen=True, slots=True)
class FrozenImageToolResourceAllowance:
    call_ordinal: int
    tool_call_id: str
    executor_binding_fingerprint: str
    canonical_bytes: int
    logical_bytes: int
    wire_bytes: int
    input_tokens: int
    quote_owner: "ImageToolResourceQuotePort" = field(
        repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if (
            isinstance(self.call_ordinal, bool)
            or self.call_ordinal < 0
            or not self.tool_call_id
            or not self.executor_binding_fingerprint
            or min(
                self.canonical_bytes,
                self.logical_bytes,
                self.wire_bytes,
                self.input_tokens,
            )
            < 0
        ):
            raise ValueError("image Tool resource allowance is invalid")


@dataclass(frozen=True, slots=True)
class FrozenImageToolResourceIncrement:
    canonical_bytes: int
    logical_bytes: int
    wire_bytes: int
    input_tokens: int

    def __post_init__(self) -> None:
        if min(
            self.canonical_bytes,
            self.logical_bytes,
            self.wire_bytes,
            self.input_tokens,
        ) < 0:
            raise ValueError("image Tool resource increment is invalid")


class ImageToolResourceQuotePort(Protocol):
    def quote(
        self,
        *,
        tool_call_id: str,
        source: ViewImageSource | VisualizationSource,
        content: FrozenPromptContent,
    ) -> FrozenImageToolResourceIncrement: ...


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
    CAPABILITY_FORM_REQUIRED = "CAPABILITY_FORM_REQUIRED"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
    CANCELLED_BEFORE_DISPATCH = "CANCELLED_BEFORE_DISPATCH"


@dataclass(frozen=True, slots=True)
class KernelToolAuthorization:
    kind: KernelToolAuthorizationKind
    reference: str
    public_message: str = ""
    capability_call: CapabilityManagementCall | None = field(default=None, repr=False, compare=False, kw_only=True)
    capability_permission_required: bool = field(default=False, kw_only=True)
    capability_user_submission: bool = field(default=False, kw_only=True)
    accepted_attempt_id: str | None = None
    accepted_result_entry_id: str | None = None
    accepted_permission_snapshot_fingerprint: str | None = None
    accepted_result_id: str | None = None
    accepted_result_entry_sequence: int | None = None
    accepted_result_observed_at: datetime | None = None
    accepted_result_public_body: str | None = field(default=None, repr=False)

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
        if (self.accepted_result_entry_id is not None) != all(
            value is not None
            for value in (
                self.accepted_result_id,
                self.accepted_result_entry_sequence,
                self.accepted_result_observed_at,
                self.accepted_result_public_body,
            )
        ):
            raise ValueError("accepted no-attempt ToolResult facts are incomplete")


@dataclass(frozen=True, slots=True)
class PreparedResolvedToolInvocation:
    requested_tool_name: str
    canonical_tool_name: str
    external_tool_name: str
    pulsara_tool_name: str | None
    resolved_arguments: FrozenJsonObjectFact

    def __post_init__(self) -> None:
        if not all(
            (
                self.requested_tool_name,
                self.canonical_tool_name,
                self.external_tool_name,
            )
        ) or not isinstance(self.resolved_arguments, FrozenJsonObjectFact):
            raise ValueError("prepared resolved Tool invocation is invalid")


@dataclass(frozen=True, slots=True)
class PreparedToolPreparationRejection:
    requested_tool_name: str
    post_tool_name: str
    post_external_tool_name: str
    post_pulsara_tool_name: str | None
    post_arguments: FrozenJsonObjectFact
    authorization: KernelToolAuthorization

    def __post_init__(self) -> None:
        if (
            not self.requested_tool_name
            or not self.post_tool_name
            or not self.post_external_tool_name
            or self.authorization.kind
            not in {
                KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
            }
            or not isinstance(self.post_arguments, FrozenJsonObjectFact)
        ):
            raise ValueError("prepared Tool rejection is invalid")


type PreparedToolInvocation = (
    PreparedResolvedToolInvocation | PreparedToolPreparationRejection
)


@dataclass(frozen=True, slots=True)
class PreparedPermissionRequest:
    """Exact process-local ASK carrier; it is neither a decision nor a permit."""

    tool_call_id: str
    turn_id: str
    scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    request_nonce: object = field(repr=False, compare=False)
    pending_state_key: object | None = field(repr=False, compare=False)
    pending_admission: object | None = field(repr=False, compare=False)
    pending_permit: object | None = field(repr=False, compare=False)
    pending_meta_invocation: object | None = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            not self.tool_call_id
            or not self.turn_id
            or (self.scope_kind is ModelInputScopeKind.ROOT)
            != (self.scope_subagent_task_id is None)
            or (self.pending_state_key is None)
            != (self.pending_admission is None and self.pending_permit is None)
            or (
                self.pending_meta_invocation is not None
                and self.pending_state_key is None
            )
        ):
            raise ValueError("prepared permission request is inconsistent")


@dataclass(frozen=True, slots=True)
class FrozenToolResultPublicProjectionInput:
    canonical_body: str = field(repr=False)
    metadata: ProviderToolResultContextMetadata
    delivery: FrozenToolResultDeliveryRequirement

    def __post_init__(self) -> None:
        self.canonical_body.encode("utf-8")
        if not isinstance(self.metadata, ProviderToolResultContextMetadata):
            raise TypeError("ToolResult public metadata must be frozen")
        if not isinstance(self.delivery, FrozenToolResultDeliveryRequirement):
            raise TypeError("ToolResult delivery requirement must be frozen")


@dataclass(frozen=True, slots=True)
class AcceptedCanonicalToolResultSettlement:
    scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    turn_id: str
    assistant_entry_id: str
    call_ordinal: int
    tool_name: str
    tool_call_id: str
    public_arguments: FrozenJsonObjectFact
    result_id: str
    result_entry_id: str
    accepted_entry_sequence: int
    result_state: str
    result_origin_kind: str
    public_projection: FrozenToolResultPublicProjectionInput
    canonical_content: FrozenPromptContent | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (
            (self.scope_kind is ModelInputScopeKind.ROOT)
            != (self.scope_subagent_task_id is None)
            or not all(
                (
                    self.turn_id,
                    self.assistant_entry_id,
                    self.tool_name,
                    self.tool_call_id,
                    self.result_id,
                    self.result_entry_id,
                    self.result_state,
                )
            )
            or self.call_ordinal < 0
            or self.accepted_entry_sequence < 1
            or self.result_origin_kind
            not in {"PHYSICAL_ATTEMPT", "POLICY_NO_ATTEMPT", "PLAN_CONTROL"}
            or not isinstance(self.public_arguments, FrozenJsonObjectFact)
        ):
            raise ValueError("accepted canonical ToolResult settlement is invalid")
        if self.canonical_content is not None:
            if not isinstance(self.canonical_content, FrozenPromptContent):
                raise TypeError("canonical ToolResult content must be frozen")
            if (
                frozen_tool_result_public_text(self.canonical_content)
                != self.public_projection.canonical_body
            ):
                raise ValueError("ToolResult public projection drifted from canonical content")


def build_accepted_canonical_tool_result_settlement(
    *,
    session_id: str,
    scope_kind: ModelInputScopeKind,
    scope_subagent_task_id: str | None,
    turn_id: str,
    assistant_entry_id: str,
    call_ordinal: int,
    tool_name: str,
    tool_call_id: str,
    public_arguments: FrozenJsonObjectFact,
    result_id: str,
    result_entry_id: str,
    accepted_entry_sequence: int,
    result_state: str,
    result_origin_kind: str,
    canonical_body: str,
    observed_at: datetime,
    observation_origin: ToolObservationOrigin,
    observation_duration_microseconds: int | None = None,
    tool_reported_duration_microseconds: int | None = None,
    display_kind: ToolResultDisplayKind = ToolResultDisplayKind.COMPLETE,
    artifact_disposition: ToolOutputArtifactDisposition = (
        ToolOutputArtifactDisposition.NOT_REQUIRED
    ),
    artifact_id: str | None = None,
    source_coverage: ToolOutputSourceCoverage = ToolOutputSourceCoverage.COMPLETE,
    source_coverage_reason: ToolOutputSourceCoverageReason | None = None,
    artifact_unavailability_reason: (
        ToolOutputArtifactUnavailabilityReason | None
    ) = None,
    model_visible_memory_fact_ids: tuple[str, ...] = (),
    canonical_content: FrozenPromptContent | None = None,
) -> AcceptedCanonicalToolResultSettlement:
    """Freeze the one post-FULL, repository-neutral ToolResult carrier."""

    timing = freeze_tool_observation_timing_fact(
        session_id=session_id,
        turn_id=turn_id,
        observed_at=observed_at,
        observation_duration_microseconds=observation_duration_microseconds,
        tool_reported_duration_microseconds=tool_reported_duration_microseconds,
        observation_origin=observation_origin,
    )
    metadata = ProviderToolResultContextMetadata(
        result_id=result_id,
        result_state=result_state,
        display_kind=display_kind,
        artifact_disposition=artifact_disposition,
        artifact_id=artifact_id,
        source_coverage=source_coverage,
        source_coverage_reason=source_coverage_reason,
        artifact_unavailability_reason=artifact_unavailability_reason,
        model_visible_memory_fact_ids=model_visible_memory_fact_ids,
        timing=timing,
    )
    return AcceptedCanonicalToolResultSettlement(
        scope_kind=scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        turn_id=turn_id,
        assistant_entry_id=assistant_entry_id,
        call_ordinal=call_ordinal,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        public_arguments=public_arguments,
        result_id=result_id,
        result_entry_id=result_entry_id,
        accepted_entry_sequence=accepted_entry_sequence,
        result_state=result_state,
        result_origin_kind=result_origin_kind,
        public_projection=FrozenToolResultPublicProjectionInput(
            canonical_body=canonical_body,
            metadata=metadata,
            delivery=classify_tool_result_delivery(
                tool_name=tool_name,
                result_state=result_state,
                has_image_attachment=(
                    canonical_content is not None
                    and any(
                        isinstance(part, LLMImagePart)
                        for part in canonical_content.parts
                    )
                ),
            ),
        ),
        canonical_content=canonical_content,
    )


class ToolSurfacePlanningPort(Protocol):
    def snapshot_terminal_cwd(self) -> Path: ...

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

    def issue_capability_dispatch_observation(
        self,
        *,
        capability_dispatch_cut: FrozenCapabilityDispatchCut,
        prepared_surface: PreparedKernelToolSurface,
        surface_borrow: ProcessLocalToolSurfaceBorrow,
    ) -> object: ...

    def assert_no_tool_surface_borrows(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
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
    def snapshot_terminal_cwd(self) -> Path: ...

    def prepare_resolved_invocation(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        surface_borrow: ProcessLocalToolSurfaceBorrow,
    ) -> PreparedToolInvocation: ...

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
        prepared_request: PreparedPermissionRequest,
        tool_name: str,
        assistant_entry_id: str,
        permission_snapshot: FrozenRunPermissionSnapshot,
    ) -> KernelToolAuthorization: ...

    async def request_capability_form(
        self, *, authorization: KernelToolAuthorization, turn_id: str,
        assistant_entry_id: str, tool_call_id: str,
        permission_snapshot: FrozenRunPermissionSnapshot,
    ) -> KernelToolAuthorization: ...

    def prepare_permission_request(
        self,
        *,
        tool_call_id: str,
        turn_id: str,
        surface_borrow: ProcessLocalToolSurfaceBorrow,
    ) -> PreparedPermissionRequest: ...

    def resolve_hook_permission(
        self, *, prepared_request: PreparedPermissionRequest, allow: bool
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
    "AcceptedCanonicalToolResultSettlement",
    "build_accepted_canonical_tool_result_settlement",
    "FrozenImageToolResourceAllowance",
    "FrozenImageToolResourceIncrement",
    "FrozenToolResultPublicProjectionInput",
    "KernelToolAuthorization",
    "KernelToolAuthorizationKind",
    "KernelToolInvocationContext",
    "KernelToolLiveSink",
    "KernelToolPhysicalInvocationError",
    "KernelToolResult",
    "ImageToolResourceQuotePort",
    "ProcessLocalEffectSettlementDisposition",
    "ProcessLocalEffectSettlementOutcome",
    "ProcessLocalEffectSettlementResult",
    "ProcessLocalEffectSettlementToken",
    "PreparedResolvedToolInvocation",
    "PreparedPermissionRequest",
    "PreparedToolInvocation",
    "PreparedToolPreparationRejection",
    "ToolInvocationPort",
    "ToolSurfacePlanningPort",
]
