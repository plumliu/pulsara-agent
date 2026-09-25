"""Host-scoped tool surface with process-local physical execution ownership."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Condition, Lock, RLock
from time import monotonic
from typing import Callable, Mapping, Protocol
from uuid import uuid4

from jsonschema import ValidationError, validators

from .capability_management import CapabilityManagementPreparation
from .capability_management_execution import CapabilityManagementCall
from pulsara_agent.capability.mcp_management import McpManagementConflict
from pulsara_agent.capability.management_form import PendingCapabilityForm, AcceptedCapabilityFormSubmission

from pulsara_agent.capability.builtin_catalog import (
    BuiltinToolCatalogEntry,
    builtin_availability_requirement_identity_fingerprint,
    builtin_permission_contract_identity_fingerprint,
    builtin_tool_catalog_entry,
)
from pulsara_agent.capability.pulsara_home import (
    PulsaraHomeResolution,
    UserHomeResolution,
    resolve_pulsara_home,
    resolve_user_home,
)
from pulsara_agent.capability.contracts import (
    CapabilityKind,
    FrozenCapabilityDispatchCut,
    CapabilitySourceKind,
    CapabilitySourceRefreshMode,
    CapabilitySourceSnapshotDisposition,
    FrozenToolCapabilityExposurePlan,
    ToolCapabilityRouteKind,
    ToolCapabilityOrigin,
    capability_identity,
    capability_source_ref,
    capability_source_registration,
    freeze_capability_source_snapshot,
    freeze_tool_capability_fact,
    tool_capability_version_ref,
)
from pulsara_agent.capability.mcp_projection import (
    McpInspectionDescriptorValues,
    conservative_mcp_inspection_logical_utf8_bytes,
    render_inspected_new_mcp_tool_provider_result,
)
from pulsara_agent.model_input.contracts import (
    FrozenCanonicalCompileSnapshot,
    FrozenCompiledModelInput,
    FrozenToolSpec,
    ModelInputScopeKind,
    ToolResultProviderRenderMode,
    compiled_tool_result_source_fingerprint,
)
from pulsara_agent.model_input.continuity import (
    ProcessLocalProviderInputInstallPermit,
)
from pulsara_agent.message import ToolResultState
from pulsara_agent.mcp_config import McpServerConfig
from pulsara_agent.ports.artifact import ToolArtifactReadPort
from pulsara_agent.ports.terminal import (
    TerminalMonitorRegisterInput,
    parse_terminal_input,
    parse_terminal_monitor_input,
    parse_terminal_process_input,
)
from pulsara_agent.ports.tool_execution import (
    Tool,
    ToolCall,
    ToolExecutionResult,
    ToolOutputArtifactCandidate,
    ToolOutputSourceCoverage,
    ToolOutputSourceFormatHint,
)
from pulsara_agent.ports.tool_registry import (
    tool_binding_contract_identity_fingerprint,
)
from pulsara_agent.hooks.executor import HookSecretScrubSet
from pulsara_agent.terminal_process import (
    TerminalCwdScope,
    TerminalProcessInfo,
    TerminalProcessOrigin,
    TerminalRequest,
    TerminalResult,
    TerminalManager,
)
from pulsara_agent.terminal_process.output import (
    InvalidTerminalOutputCursor,
    TerminalOutputSnapshot,
)
from pulsara_agent.terminal_process.monitor import (
    PreparedTerminalMonitorRegistration,
    TerminalMonitorCoordinator,
    TerminalMonitorPolicy,
    TerminalMonitorRejected,
)
from pulsara_agent.tools.builtins.filesystem import (
    EditFileTool,
    LocalImageReadCandidate,
    ReadFileTool,
    SearchFilesTool,
    ViewImageSourceKind,
    ViewImageTool,
    WriteFileTool,
    parse_view_image_source,
)
from pulsara_agent.tools.builtins.visualization import VisualizationRenderTool
from pulsara_agent.conversation_kernel.visualization import (
    PostgresCanonicalVisualizationReadPort,
    VisualizationSourceKind,
    VisualizationSubscription,
    parse_visualization_source,
    read_visualization_file,
)
from pulsara_agent.conversation_kernel.visualization_screenshot import (
    VisualizationScreenshotError,
    VisualizationScreenshotOwner,
)
from pulsara_agent.conversation_kernel.prompt_storage import (
    CanonicalImageReferenceReadPort,
    CanonicalImageReferenceResourceExceeded,
    CanonicalImageReferenceUnavailable,
)
from pulsara_agent.conversation_kernel.image_validation import (
    HostPromptImageValidator,
    PromptImageValidationError,
)
from pulsara_agent.llm.input import (
    FrozenPromptContent,
    LLMImagePart,
    LLMTextPart,
)
from pulsara_agent.tools.builtins.workspace import WritePathScope
from pulsara_agent.tools.builtins.todo import (
    TodoTool,
    TodoValidationError,
    parse_todo_replacement,
)
from pulsara_agent.tools.builtins.artifact import ArtifactReadTool
from pulsara_agent.conversation_kernel.io import (
    KernelSessionIO,
    PhysicalToolInvocationDisposition,
    PhysicalToolInvocationTiming,
)
from pulsara_agent.conversation_kernel.compaction.runtime_handoff import (
    FrozenCompactionRuntimeHandoff,
    FrozenTerminalMonitorHandoffFact,
    FrozenTerminalProcessHandoffFact,
    bounded_handoff_preview,
    freeze_compaction_runtime_handoff,
)
from pulsara_agent.conversation_kernel.capability_composition import (
    BuiltinCompositionState,
    SealedBuiltinCapabilitySnapshot,
    issue_sealed_builtin_capability_snapshot,
)
from pulsara_agent.conversation_kernel.interaction_arbiter import (
    InteractionAdmissionHooks,
)
from pulsara_agent.conversation_kernel.memory.contracts import (
    FrozenModelCallMemoryContext,
)
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
    KernelWatchdogOwner,
)
from pulsara_agent.conversation_kernel.live import (
    LiveAgentEventBus,
    LiveBlockKind,
    LiveChannelKind,
)
from pulsara_agent.ports.live_agent_event import (
    TerminalProcessCompletedPayload,
    TodoLiveItemProjection,
    TodoSnapshotUpdatedPayload,
    live_digest,
)
from pulsara_agent.primitives.model_call import sha256_fingerprint
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    canonical_json_bytes,
    context_fingerprint,
    freeze_json,
    thaw_json,
)
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot
from pulsara_agent.primitives.tool_observation import (
    MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES,
    PhysicalToolObservationSupplement,
    ToolObservationOrigin,
    TrustedToolObservationSupplement,
    normalize_observation_duration,
)
from pulsara_agent.primitives.tool_result_projection import (
    ToolResultFullDeliveryReason,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.conversation_kernel.vocabulary import LiveEventType
from pulsara_agent.conversation_kernel.tool_policy import (
    ToolDispatchAuthorizationPolicy,
    ToolDispatchAuthorizationRequest,
    ToolDispatchDecisionKind,
)
from pulsara_agent.conversation_kernel.tool_surface import (
    BuiltinExecutionPolicyRef,
    DirectToolAccessLeaf,
    McpEffectKind,
    McpToolExecutionPolicyFact,
    PreparedKernelToolSurface,
    PreparedToolExecutionBinding,
    PreparedUnavailableDirectMcpGate,
    ProcessLocalToolSurfaceAccess,
    ProcessLocalToolSurfaceBorrow,
    execution_policy_fingerprint,
    tool_observation_origin_for_binding,
)

from .tool_contracts import (
    KernelToolInvocationContext,
    KernelToolLiveSink,
    KernelToolAuthorization,
    KernelToolAuthorizationKind,
    KernelToolPhysicalInvocationError,
    KernelToolResult,
    ProcessLocalEffectSettlementDisposition,
    ProcessLocalEffectSettlementOutcome,
    ProcessLocalEffectSettlementResult,
    ProcessLocalEffectSettlementToken,
    PreparedResolvedToolInvocation,
    PreparedPermissionRequest,
    PreparedToolInvocation,
    PreparedToolPreparationRejection,
)
from .todo_runtime import (
    FrozenTodoCloseProjection,
    PreparedTodoReplacement,
    TodoInstallation,
    TodoRunStateOwner,
)
from .mcp.supervisor import (
    McpBoundToolExecutor,
    McpDispatchAdmissionPermit,
    McpHostSupervisor,
    McpInstalledRuntimeGeneration,
    McpKnownToolResult,
    McpPhysicalOutcomeUnknown,
    McpSnapshotStale,
)
from .mcp.contracts import McpDiscoveryCatalogInspection
from .mcp.directory import McpDirectoryPageFactory
from .mcp.meta import (
    McpToolRefCapacityExceeded,
    NewMcpToolRef,
    PreparedNewMcpToolRefSettlement,
    ProcessLocalNewMcpToolRefOwner,
)


_TERMINAL_PROCESS_ACTION_EFFECTS = (
    ("list", "TERMINAL_OBSERVATION"),
    ("poll", "TERMINAL_OBSERVATION"),
    ("wait", "TERMINAL_OBSERVATION"),
    ("write", "TERMINAL_EFFECT"),
    ("submit", "TERMINAL_EFFECT"),
    ("close_stdin", "TERMINAL_EFFECT"),
    ("kill", "TERMINAL_EFFECT"),
)


@dataclass(frozen=True, slots=True)
class ProductionBuiltinExecutorBinding:
    """Exact descriptor-to-executor closure for one advertised builtin."""

    catalog_entry: BuiltinToolCatalogEntry
    executor_identity: str

    @property
    def tool_name(self) -> str:
        return self.catalog_entry.name


def _builtin_physical_effect_contract(
    entry: BuiltinToolCatalogEntry,
) -> object:
    return (
        {"actions": _TERMINAL_PROCESS_ACTION_EFFECTS}
        if entry.name == "terminal_process"
        else {
            "default": (
                "TERMINAL_EFFECT"
                if entry.name == "terminal"
                else entry.recovery_contract.severity
            )
        }
    )


def production_builtin_executor_binding_identity_fingerprint(
    binding: ProductionBuiltinExecutorBinding,
) -> str:
    """Derive the stable physical binding identity at its actual consumers."""

    entry = binding.catalog_entry
    descriptor = entry.descriptor
    contract = entry.binding_contract
    input_schema_fingerprint = sha256_fingerprint(
        "production-builtin-input-schema:v1", descriptor.input_schema
    )
    physical_effect_contract_fingerprint = sha256_fingerprint(
        "production-builtin-physical-effect-contract:v1",
        _builtin_physical_effect_contract(entry),
    )
    return sha256_fingerprint(
        "production-builtin-executor-binding:v1",
        {
            "tool_name": entry.name,
            "descriptor_id": descriptor.id,
            "descriptor_contract_version": contract.contract_version,
            "descriptor_fingerprint": descriptor.fingerprint(),
            "input_schema_fingerprint": input_schema_fingerprint,
            "binding_contract_fingerprint": (
                tool_binding_contract_identity_fingerprint(contract)
            ),
            "catalog_entry_fingerprint": entry.entry_fingerprint,
            "availability_requirement_fingerprint": (
                builtin_availability_requirement_identity_fingerprint(
                    entry.availability_requirement
                )
            ),
            "permission_contract_fingerprint": (
                builtin_permission_contract_identity_fingerprint(
                    entry.permission_contract
                )
            ),
            "execution_binding_kind": entry.execution_binding_kind.value,
            "is_read_only": descriptor.is_read_only,
            "is_concurrency_safe": descriptor.is_concurrency_safe,
            "permission_category": descriptor.permission_category,
            "physical_effect_contract_fingerprint": (
                physical_effect_contract_fingerprint
            ),
            "executor_identity": binding.executor_identity,
        },
    )


def _production_executor_binding(
    tool_name: str, executor_identity: str
) -> ProductionBuiltinExecutorBinding:
    if not tool_name or not executor_identity:
        raise ValueError("production executor binding identity is incomplete")
    entry = builtin_tool_catalog_entry(tool_name)
    descriptor = entry.descriptor
    contract = entry.binding_contract
    if descriptor.name != tool_name or contract.tool_name != tool_name:
        raise RuntimeError("builtin descriptor and executor name do not join")
    return ProductionBuiltinExecutorBinding(
        catalog_entry=entry,
        executor_identity=executor_identity,
    )


def _qualified_executor_identity(owner: object, tool_name: str) -> str:
    owner_type = type(owner)
    return f"{owner_type.__module__}.{owner_type.__qualname__}#{tool_name}"


class KernelSubagentToolPort(Protocol):
    @property
    def tool_names(self) -> frozenset[str]: ...

    def validate_arguments(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> str | None: ...

    async def invoke(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        invocation_context: KernelToolInvocationContext,
    ) -> KernelToolResult: ...

    async def freeze_compaction_handoff(self) -> tuple[dict[str, str], ...]: ...


class KernelMemoryToolPort(Protocol):
    @property
    def tool_names(self) -> frozenset[str]: ...

    async def invoke(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        invocation_context: KernelToolInvocationContext,
    ) -> KernelToolResult: ...


class KernelToolInteractionResolution(Protocol):
    capability_submission: AcceptedCapabilityFormSubmission | None
    decision: str
    reference: str
    public_message: str
    attempt_id: str | None
    result_entry_id: str | None


class KernelToolInteractionPort(Protocol):
    async def request_capability_form(self, *, turn_id: str, assistant_entry_id: str,
        tool_call_id: str, permission_snapshot: FrozenRunPermissionSnapshot,
        form: PendingCapabilityForm) -> KernelToolInteractionResolution: ...

    async def request_tool_confirmation(
        self,
        *,
        turn_id: str,
        assistant_entry_id: str,
        tool_call_id: str,
        tool_name: str,
        permission_snapshot: FrozenRunPermissionSnapshot,
        admission_hooks: InteractionAdmissionHooks | None = None,
    ) -> KernelToolInteractionResolution: ...

    async def cancel_tool_confirmations(
        self,
        *,
        owner_keys: frozenset[str],
        reference: str,
        public_message: str,
    ) -> None: ...


class KernelCapabilityReloadPort(Protocol):
    async def adopt_capability_management_change(
        self, *, workspace_root: Path | None
    ) -> Mapping[str, object]: ...

    async def reload_hooks(
        self, *, deadline_monotonic: float | None
    ) -> Mapping[str, object]: ...

    async def reload_capabilities(
        self, *, deadline_monotonic: float | None
    ) -> Mapping[str, object]: ...


@dataclass(slots=True)
class _DirectTerminalTool:
    manager: TerminalManager
    owner_host_session_id: str
    name: str = "terminal"

    def execute(
        self,
        call: ToolCall,
        *,
        live_sink: KernelToolLiveSink | None = None,
        origin: TerminalProcessOrigin,
        decision_attempt_id: str,
        decision_deadline_monotonic: float,
        effective_permission_mode: PermissionMode,
    ) -> ToolExecutionResult:
        request = parse_terminal_input(call.arguments)
        result = self.manager.execute(
            TerminalRequest(
                command=request.command,
                workdir=request.workdir,
                yield_time_ms=request.yield_time_ms,
                max_output_chars=request.max_output_chars,
                tty=request.tty,
            ),
            owner_host_session_id=self.owner_host_session_id,
            output_subscriber=(
                None
                if live_sink is None
                else lambda value, _start, _end: live_sink.offer_text(
                    value.decode("utf-8")
                )
            ),
            origin=origin,
            decision_attempt_id=decision_attempt_id,
            decision_deadline_monotonic=decision_deadline_monotonic,
            cwd_scope=(
                TerminalCwdScope.WORKSPACE
                if effective_permission_mode is PermissionMode.READ_ONLY
                else TerminalCwdScope.HOST_LOCAL
            ),
        )
        return _terminal_execution_result(call, result)


@dataclass(slots=True)
class _DirectTerminalProcessTool:
    manager: TerminalManager
    owner_host_session_id: str
    name: str = "terminal_process"

    def execute(
        self,
        call: ToolCall,
        *,
        live_sink: KernelToolLiveSink | None = None,
    ) -> ToolExecutionResult:
        request = parse_terminal_process_input(call.arguments)
        action = request.action
        maximum = getattr(request, "max_output_chars", 32_000)
        if action == "list":
            processes = self.manager.list_processes(
                owner_host_session_id=self.owner_host_session_id,
                include_finished=request.include_finished,
                include_running=request.include_running,
            )
            return _success(
                call,
                {
                    "status": "success",
                    "terminal_process_action": action,
                    "processes": [item.to_payload() for item in processes],
                },
            )
        if action == "poll":
            result = self.manager.poll_process(
                request.process_id,
                max_output_chars=maximum,
                owner_host_session_id=self.owner_host_session_id,
                since_cursor=request.since_cursor,
            )
        elif action == "wait":
            result = self.manager.wait_process(
                request.process_id,
                timeout_seconds=request.timeout_seconds,
                max_output_chars=maximum,
                owner_host_session_id=self.owner_host_session_id,
                since_cursor=request.since_cursor,
                output_subscriber=(
                    None
                    if live_sink is None
                    else lambda value, _start, _end: live_sink.offer_text(
                        value.decode("utf-8")
                    )
                ),
            )
        elif action == "write":
            result = self.manager.write_process(
                request.process_id,
                request.data,
                append_newline=False,
                yield_time_ms=request.yield_time_ms,
                max_output_chars=maximum,
                owner_host_session_id=self.owner_host_session_id,
            )
        elif action == "submit":
            result = self.manager.write_process(
                request.process_id,
                request.data,
                append_newline=True,
                yield_time_ms=request.yield_time_ms,
                max_output_chars=maximum,
                owner_host_session_id=self.owner_host_session_id,
            )
        elif action == "close_stdin":
            result = self.manager.close_process_stdin(
                request.process_id,
                max_output_chars=maximum,
                owner_host_session_id=self.owner_host_session_id,
            )
        elif action == "kill":
            result = self.manager.kill_process(
                request.process_id,
                max_output_chars=maximum,
                owner_host_session_id=self.owner_host_session_id,
            )
        else:  # pragma: no cover - pydantic discriminator is exhaustive
            raise AssertionError(action)
        return _terminal_execution_result(call, result, action=action)


@dataclass(slots=True)
class _DirectTerminalMonitorTool:
    coordinator: TerminalMonitorCoordinator
    name: str = "terminal_monitor"

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        raise RuntimeError(
            "terminal_monitor requires the closed kernel invocation context"
        )


@dataclass(slots=True)
class _DirectPlanControlTool:
    name: str

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        del call
        raise RuntimeError("Plan control must be consumed by the runner batch barrier")


@dataclass(slots=True)
class _DirectCapabilityControlTool:
    name: str

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        del call
        raise RuntimeError("Hook control escaped its async Host adapter")


@dataclass(slots=True)
class _DirectMcpCatalogTool:
    name: str

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        del call
        raise RuntimeError("MCP catalog tool escaped its generation-bound adapter")


@dataclass(frozen=True, slots=True)
class _PendingMcpConfirmationAdmission:
    generation: int
    executor: McpBoundToolExecutor
    scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    turn_id: str
    tool_call_id: str


@dataclass(frozen=True, slots=True)
class _InstalledBorrowEpoch:
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    epoch_nonce: str
    epoch_revision: int


@dataclass(frozen=True, slots=True)
class _PreparedMcpMetaInvocation:
    executor: McpBoundToolExecutor
    ref: NewMcpToolRef
    arguments: FrozenJsonObjectFact


class DirectKernelToolPort:
    """Permission-before-attempt and process-local physical execution owner."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        host_owner_id: str,
        authorization_policy: ToolDispatchAuthorizationPolicy,
        session_id: str,
        live_bus: LiveAgentEventBus,
        artifact_read_port: ToolArtifactReadPort | None = None,
        image_reference_read_port: CanonicalImageReferenceReadPort | None = None,
        visualization_reference_read_port: PostgresCanonicalVisualizationReadPort | None = None,
        terminal_monitor_wake_scheduler: Callable[[], None] | None = None,
        deadline_factory: KernelExecutionDeadlineFactory | None = None,
        pulsara_home_resolution: PulsaraHomeResolution | None = None,
        user_home_resolution: UserHomeResolution | None = None,
        image_validator: HostPromptImageValidator | None = None,
    ) -> None:
        root = workspace_root.expanduser().resolve()
        frozen_user_home = user_home_resolution or resolve_user_home()
        frozen_pulsara_home = pulsara_home_resolution or resolve_pulsara_home(
            user_home_resolution=frozen_user_home
        )
        self._host_owner_id = host_owner_id
        self._session_id = session_id
        self._workspace_root = root
        self._live_bus = live_bus
        self._physical_io = KernelSessionIO()
        self._deadlines = deadline_factory or KernelExecutionDeadlineFactory()
        self._image_validator = image_validator
        self._image_reference_read_port = image_reference_read_port
        self._visualization_reference_read_port = visualization_reference_read_port
        self._visualization_screenshots = VisualizationScreenshotOwner()
        self._terminal = TerminalManager(
            workspace_root=root,
            completion_subscriber=self._terminal_process_completed,
        )
        self._terminal.activate_owner(host_owner_id)
        self._terminal_monitor = TerminalMonitorCoordinator(
            session_id=session_id,
            owner_epoch=host_owner_id,
            registry=self._terminal.process_registry,
            live_bus=live_bus,
            wake_scheduler=terminal_monitor_wake_scheduler or (lambda: None),
        )
        tools: tuple[Tool, ...] = (
            ReadFileTool(
                root,
                pulsara_home_resolution=frozen_pulsara_home,
                user_home_resolution=frozen_user_home,
            ),
            ViewImageTool(
                root,
                pulsara_home_resolution=frozen_pulsara_home,
                user_home_resolution=frozen_user_home,
            ),
            VisualizationRenderTool(
                root,
                pulsara_home_resolution=frozen_pulsara_home,
                user_home_resolution=frozen_user_home,
            ),
            SearchFilesTool(
                root,
                pulsara_home_resolution=frozen_pulsara_home,
                user_home_resolution=frozen_user_home,
            ),
            EditFileTool(
                root,
                pulsara_home_resolution=frozen_pulsara_home,
                user_home_resolution=frozen_user_home,
            ),
            WriteFileTool(
                root,
                pulsara_home_resolution=frozen_pulsara_home,
                user_home_resolution=frozen_user_home,
            ),
            TodoTool(),
            _DirectTerminalTool(self._terminal, host_owner_id),
            _DirectTerminalProcessTool(self._terminal, host_owner_id),
            _DirectTerminalMonitorTool(self._terminal_monitor),
            _DirectPlanControlTool("enter_plan"),
            _DirectPlanControlTool("ask_plan_question"),
            _DirectPlanControlTool("exit_plan"),
            _DirectCapabilityControlTool("reload_hooks"),
            _DirectCapabilityControlTool("reload_capabilities"),
            _DirectCapabilityControlTool("manage_capability"),
            _DirectMcpCatalogTool("list_mcp_servers"),
            _DirectMcpCatalogTool("inspect_new_mcp_tool"),
            _DirectMcpCatalogTool("use_new_mcp_tool"),
            _DirectMcpCatalogTool("list_mcp_resources"),
            _DirectMcpCatalogTool("list_mcp_resource_templates"),
            _DirectMcpCatalogTool("read_mcp_resource"),
            _DirectMcpCatalogTool("list_mcp_prompts"),
            _DirectMcpCatalogTool("get_mcp_prompt"),
        )
        if artifact_read_port is not None:
            tools = (*tools, ArtifactReadTool(artifact_read_port))
        self._tools = {tool.name: tool for tool in tools}
        self._authorization_policy = authorization_policy
        self._close_lock = Lock()
        self._surface_lock = RLock()
        self._surface_condition = Condition(self._surface_lock)
        self._surface_authority = object()
        self._surface_generation = 1
        self._surface_owner_epoch = 1
        self._surface_borrows: dict[str, int] = {}
        self._surface_borrow_owners: dict[str, ProcessLocalToolSurfaceBorrow] = {}
        self._prepared_surfaces: dict[
            tuple[int, ModelInputScopeKind, str | None], PreparedKernelToolSurface
        ] = {}
        self._closed = False
        self._builtin_composition_state = BuiltinCompositionState.PREPARING
        self._builtin_composition_seal: object | None = None
        self._sealed_builtin_bindings: tuple[
            ProductionBuiltinExecutorBinding, ...
        ] | None = None
        self._physically_closed = False
        self._terminal_physically_closed = False
        self._close_async_lock = asyncio.Lock()
        self._terminal_release_task: asyncio.Task[object] | None = None
        self._terminal_monitor_close_task: asyncio.Task[object] | None = None
        self._process_local_settlements: dict[
            str, ProcessLocalEffectSettlementToken
        ] = {}
        self._todo_settlements: dict[str, ProcessLocalEffectSettlementToken] = {}
        self._visualization_settlements: dict[
            str, ProcessLocalEffectSettlementToken
        ] = {}
        self._visualization_subscriptions: dict[
            str, dict[tuple[str, str], VisualizationSubscription]
        ] = {}
        self._mcp_ref_settlements: dict[
            str, ProcessLocalEffectSettlementToken
        ] = {}
        self._todo_owner = TodoRunStateOwner(
            session_id=session_id,
            owner_epoch=host_owner_id,
        )
        self._subagent: KernelSubagentToolPort | None = None
        self._memory: KernelMemoryToolPort | None = None
        self._interaction: KernelToolInteractionPort | None = None
        self._capability_reload: KernelCapabilityReloadPort | None = None
        self._capability_management = None

        self._mcp_supervisor: McpHostSupervisor | None = None
        self._mcp_current: McpInstalledRuntimeGeneration | None = None
        self._mcp_runtime_by_surface_generation: dict[
            int, McpInstalledRuntimeGeneration
        ] = {}
        self._mcp_dispatch_permits: dict[
            tuple[int, str], McpDispatchAdmissionPermit
        ] = {}
        self._mcp_confirmation_admissions: dict[
            tuple[int, str], _PendingMcpConfirmationAdmission
        ] = {}
        self._mcp_meta_invocations: dict[
            tuple[int, str], _PreparedMcpMetaInvocation
        ] = {}
        self._mcp_meta_refs = ProcessLocalNewMcpToolRefOwner()
        self._mcp_directory = McpDirectoryPageFactory()
        self._installed_epoch_by_borrow: dict[str, _InstalledBorrowEpoch] = {}

    def visualization_subscriptions(
        self, turn_id: str
    ) -> tuple[VisualizationSubscription, ...]:
        with self._surface_lock:
            return tuple(self._visualization_subscriptions.get(turn_id, {}).values())

    def consume_visualization_subscriptions(
        self, turn_id: str, expected: tuple[VisualizationSubscription, ...]
    ) -> None:
        with self._surface_lock:
            actual = tuple(self._visualization_subscriptions.get(turn_id, {}).values())
            if actual != expected:
                raise RuntimeError("visualization subscription batch changed")
            self._visualization_subscriptions.pop(turn_id, None)

    def discard_visualization_subscriptions(self, turn_id: str) -> None:
        with self._surface_lock:
            self._visualization_subscriptions.pop(turn_id, None)

    def bind_subagent_port(self, port: KernelSubagentToolPort) -> None:
        with self._surface_lock:
            self._require_builtin_composition_preparing_locked()
            if self._subagent is not None:
                raise RuntimeError("subagent tool port is already bound")
            self._subagent = port
            self._surface_generation += 1

    def bind_memory_port(self, port: KernelMemoryToolPort) -> None:
        with self._surface_lock:
            self._require_builtin_composition_preparing_locked()
            if self._memory is not None:
                raise RuntimeError("memory tool port is already bound")
            self._memory = port
            self._surface_generation += 1

    def offer_memory_embedding_wake(self) -> None:
        memory = self._memory
        if memory is not None:
            memory.offer_embedding_wake()

    def bind_interaction_port(self, port: KernelToolInteractionPort) -> None:
        with self._surface_lock:
            self._require_builtin_composition_preparing_locked()
            if self._interaction is not None:
                raise RuntimeError("interaction tool port is already bound")
            self._interaction = port

    def bind_capability_reload_port(self, port: KernelCapabilityReloadPort) -> None:
        with self._surface_lock:
            self._require_builtin_composition_preparing_locked()
            if self._capability_reload is not None:
                raise RuntimeError("Capability reload port is already bound")
            self._capability_reload = port

    def bind_capability_management(self, service: CapabilityManagementPreparation) -> None:
        with self._surface_lock:
            self._require_builtin_composition_preparing_locked()
            if self._capability_management is not None:
                raise RuntimeError("Capability management is already bound")
            self._capability_management = service

    def bind_mcp_supervisor(self, supervisor: McpHostSupervisor) -> None:
        with self._surface_lock:
            self._require_builtin_composition_preparing_locked()
            if self._mcp_supervisor is not None:
                raise RuntimeError("MCP supervisor is already bound")
            if self._closed:
                raise RuntimeError("tool surface is closed")
            self._mcp_supervisor = supervisor

    def _require_builtin_composition_preparing_locked(self) -> None:
        if self._builtin_composition_state is not BuiltinCompositionState.PREPARING:
            raise RuntimeError("builtin composition is already sealed or closed")

    def seal_builtin_composition(self) -> object:
        with self._surface_lock:
            if self._builtin_composition_state is BuiltinCompositionState.CLOSED:
                raise RuntimeError("builtin composition is closed")
            if self._builtin_composition_state is BuiltinCompositionState.SEALED:
                assert self._builtin_composition_seal is not None
                return self._builtin_composition_seal
            if any(
                item is None
                for item in (
                    self._interaction,
                    self._subagent,
                    self._memory,
                    self._mcp_supervisor,
                    self._capability_reload,
                )
            ):
                raise RuntimeError("builtin composition required ports are incomplete")
            self._sealed_builtin_bindings = self._executor_bindings_locked()
            self._builtin_composition_seal = object()
            self._builtin_composition_state = BuiltinCompositionState.SEALED
            return self._builtin_composition_seal

    def sealed_builtin_capability_snapshot(
        self,
        *,
        conversation_scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
    ) -> SealedBuiltinCapabilitySnapshot:
        if (conversation_scope_kind is ModelInputScopeKind.ROOT) != (
            scope_subagent_task_id is None
        ):
            raise ValueError("builtin capability scope identity is invalid")
        with self._surface_lock:
            if self._builtin_composition_state is not BuiltinCompositionState.SEALED:
                raise RuntimeError("builtin composition is not sealed")
            assert self._sealed_builtin_bindings is not None
            assert self._builtin_composition_seal is not None
            bindings = self._sealed_builtin_bindings
            if conversation_scope_kind is ModelInputScopeKind.SUBAGENT_TASK:
                bindings = tuple(
                    item
                    for item in bindings
                    if item.tool_name
                    not in {
                        "remember",
                        "mark_memory_relation",
                        "terminal_monitor",
                        "enter_plan",
                        "ask_plan_question",
                        "exit_plan",
                        "reload_hooks",
                        "reload_capabilities",
                        "spawn_agent",
                        "create_agent_tasks",
                        "list_agents",
                        "wait_agent",
                        "send_agent_message",
                        "stop_agent",
                        "visualization_render",
                    }
                )
            else:
                bindings = tuple(
                    item for item in bindings if item.tool_name != "report_agent_result"
                )
            source = capability_source_ref(
                CapabilitySourceKind.BUILTIN_REGISTRY,
                "pulsara-builtin-tools",
            )
            registration = capability_source_registration(
                source=source,
                refresh_mode=CapabilitySourceRefreshMode.IMMUTABLE,
                source_contract_fingerprint=context_fingerprint(
                    "builtin-capability-source-contract:v1",
                    tuple(
                        (
                            item.tool_name,
                            item.catalog_entry.descriptor.fingerprint(),
                            tool_binding_contract_identity_fingerprint(
                                item.catalog_entry.binding_contract
                            ),
                        )
                        for item in bindings
                    ),
                ),
            )
            facts = []
            for binding in bindings:
                entry = builtin_tool_catalog_entry(binding.tool_name)
                if entry is not binding.catalog_entry:
                    raise RuntimeError("builtin catalog/executor binding drifted")
                schema = freeze_json(_json_schema_value(entry.descriptor.input_schema))
                if not isinstance(schema, FrozenJsonObjectFact):
                    raise TypeError("builtin tool schema did not freeze to an object")
                spec = FrozenToolSpec(
                    name=binding.tool_name,
                    description=entry.descriptor.description,
                    parameters=schema,
                    descriptor_fingerprint=entry.descriptor.fingerprint(),
                )
                facts.append(
                    freeze_tool_capability_fact(
                        identity=capability_identity(
                            kind=CapabilityKind.TOOL,
                            source=source,
                            stable_name=binding.tool_name,
                        ),
                        origin=ToolCapabilityOrigin.BUILTIN,
                        canonical_tool_spec=spec,
                    )
                )
            source_snapshot = freeze_capability_source_snapshot(
                registration=registration,
                conversation_scope_kind=conversation_scope_kind,
                scope_subagent_task_id=scope_subagent_task_id,
                disposition=CapabilitySourceSnapshotDisposition.COMPLETE,
                facts=tuple(facts),
            )
            return issue_sealed_builtin_capability_snapshot(
                conversation_scope_kind=conversation_scope_kind,
                scope_subagent_task_id=scope_subagent_task_id,
                source_snapshot=source_snapshot,
                executor_bindings=bindings,
                builtin_composition_seal=self._builtin_composition_seal,
            )

    def prepare_tool_surface_safe_point(self) -> None:
        supervisor = self._mcp_supervisor
        if supervisor is None:
            return
        installed = supervisor.install_pending_at_safe_point()
        if installed is None:
            return
        with self._surface_lock:
            previous_generation = self._surface_generation
            previous = self._mcp_current
            self._surface_generation += 1
            self._mcp_current = installed
            self._mcp_runtime_by_surface_generation[self._surface_generation] = (
                installed
            )
            if (
                previous is not None
                and previous_generation not in self._surface_borrows.values()
            ):
                for key in tuple(self._prepared_surfaces):
                    if key[0] == previous_generation:
                        self._prepared_surfaces.pop(key, None)
                self._mcp_runtime_by_surface_generation.pop(previous_generation, None)
                previous.release()

    def freeze_mcp_capability_source_snapshot_set(
        self,
        *,
        conversation_scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
    ):
        supervisor = self._mcp_supervisor
        if supervisor is None:
            raise RuntimeError("MCP supervisor is not bound")
        return supervisor.freeze_capability_source_snapshot_set(
            conversation_scope_kind=conversation_scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
        )

    def inspect_mcp_discovery_catalog(self) -> McpDiscoveryCatalogInspection:
        supervisor = self._mcp_supervisor
        if supervisor is None:
            raise RuntimeError("MCP supervisor is not bound")
        return supervisor.inspect_discovery_catalog()

    def freeze_mcp_capability_projection_input(self, owner):
        supervisor = self._mcp_supervisor
        if supervisor is None:
            raise RuntimeError("MCP supervisor is not bound")
        return supervisor.freeze_capability_projection_input(owner)

    async def reload_mcp_configs(
        self,
        configs: tuple[McpServerConfig, ...],
        *,
        deadline_monotonic: float,
    ) -> frozenset[str]:
        """Fence a config epoch and cancel only not-yet-FULL confirmations."""

        supervisor = self._mcp_supervisor
        if supervisor is None:
            raise RuntimeError("MCP supervisor is not bound")
        if monotonic() >= deadline_monotonic:
            raise TimeoutError("MCP config reload deadline expired before cut")
        old_configs = {item.server_id: item for item in supervisor.configs}
        new_configs = {item.server_id: item for item in configs}
        changed = supervisor.reload_configs(configs)
        if not changed:
            return changed
        disabled_or_removed = frozenset(
            server_id
            for server_id in changed
            if old_configs.get(server_id) is not None
            and old_configs[server_id].enabled
            and (server_id not in new_configs or not new_configs[server_id].enabled)
        )
        owner_keys = frozenset(
            f"mcp-server:{server_id}" for server_id in disabled_or_removed
        )
        interaction = self._interaction
        if interaction is not None and owner_keys:
            await interaction.cancel_tool_confirmations(
                owner_keys=owner_keys,
                reference="interaction:mcp-config-changed",
                public_message=(
                    "MCP confirmation ended because its server configuration changed"
                ),
            )
        for key, admission in tuple(self._mcp_confirmation_admissions.items()):
            if admission.executor.semantic.server_id in disabled_or_removed:
                self._mcp_confirmation_admissions.pop(key, None)
                self._mcp_meta_invocations.pop(key, None)
        for key, permit in tuple(self._mcp_dispatch_permits.items()):
            if (
                permit.lease._slot.server_id in disabled_or_removed  # noqa: SLF001
                and permit.state.value == "ADMITTED"
            ):
                self._mcp_dispatch_permits.pop(key, None)
                self._mcp_meta_invocations.pop(key, None)
                permit.release()
        return changed

    @property
    def terminal_monitor_coordinator(self) -> TerminalMonitorCoordinator:
        return self._terminal_monitor

    def list_background_terminal_processes(self):
        return self._terminal.list_background_processes(
            owner_host_session_id=self._host_owner_id
        )

    def read_background_terminal_log(
        self, process_id: str, *, maximum_chars: int, since_cursor: str | None = None
    ):
        return self._terminal.log_process(
            process_id,
            max_output_chars=maximum_chars,
            owner_host_session_id=self._host_owner_id,
            since_cursor=since_cursor,
        )

    def terminate_background_terminal_process(
        self,
        process_id: str,
        *,
        maximum_chars: int = 32_000,
        deadline_monotonic: float | None = None,
    ):
        del deadline_monotonic
        return self._terminal.terminate_process_if_running(
            process_id,
            max_output_chars=maximum_chars,
            owner_host_session_id=self._host_owner_id,
        )

    @property
    def todo_owner(self) -> TodoRunStateOwner:
        return self._todo_owner

    def _executor_bindings_locked(self) -> tuple[ProductionBuiltinExecutorBinding, ...]:
        if self._sealed_builtin_bindings is not None:
            return self._sealed_builtin_bindings
        identities = {
            name: _qualified_executor_identity(tool, name)
            for name, tool in self._tools.items()
        }
        if self._subagent is not None:
            for name in self._subagent.tool_names:
                if name in identities:
                    raise RuntimeError("production builtin has multiple executors")
                identities[name] = _qualified_executor_identity(self._subagent, name)
        if self._memory is not None:
            for name in self._memory.tool_names:
                if name in identities:
                    raise RuntimeError("production builtin has multiple executors")
                identities[name] = _qualified_executor_identity(self._memory, name)
        return tuple(
            _production_executor_binding(name, identities[name])
            for name in sorted(identities)
        )

    @property
    def executor_bindings(self) -> tuple[ProductionBuiltinExecutorBinding, ...]:
        with self._surface_lock:
            return self._executor_bindings_locked()

    def snapshot_workspace_root(self) -> Path:
        return self._terminal.workspace_root

    def prepare_resolved_invocation(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        surface_borrow: ProcessLocalToolSurfaceBorrow,
    ) -> PreparedToolInvocation:
        frozen_original = freeze_json(dict(arguments))
        if not isinstance(frozen_original, FrozenJsonObjectFact):
            raise TypeError("Tool arguments did not freeze to an object")

        def reject(
            kind: KernelToolAuthorizationKind,
            reference: str,
            message: str,
            *,
            post_name: str = tool_name,
            post_external_name: str = tool_name,
            post_pulsara_name: str | None = None,
            post_arguments: FrozenJsonObjectFact = frozen_original,
        ) -> PreparedToolPreparationRejection:
            return PreparedToolPreparationRejection(
                tool_name,
                post_name,
                post_external_name,
                post_pulsara_name,
                post_arguments,
                KernelToolAuthorization(kind, reference, message),
            )

        try:
            binding = self._validate_surface_borrow(surface_borrow, tool_name)
        except RuntimeError:
            return reject(
                KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                "tool-surface:revoked",
                f"tool unavailable: {tool_name}",
            )
        if isinstance(binding, PreparedUnavailableDirectMcpGate):
            return reject(
                KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                f"mcp-direct:{binding.unavailable_reason_code}",
                "MCP tool generation is unavailable",
            )
        entry = builtin_tool_catalog_entry(tool_name) if tool_name in self._tools else None
        if entry is not None:
            schema = _json_schema_value(entry.descriptor.input_schema)
            try:
                validator = validators.validator_for(schema)
                validator.check_schema(schema)
                validator(schema).validate(dict(arguments))
            except ValidationError as exc:
                return reject(
                    KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                    f"descriptor:{entry.descriptor.id}",
                    f"invalid tool arguments: {exc.message}",
                )
        if tool_name == "use_new_mcp_tool":
            token = arguments.get("tool_ref")
            inner = arguments.get("arguments")
            if not isinstance(token, str) or not isinstance(inner, Mapping):
                return reject(
                    KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                    "mcp-meta:arguments-invalid",
                    "invalid new MCP tool reference or arguments",
                )
            try:
                epoch = self._installed_epoch_for_borrow(surface_borrow)
                ref = self._mcp_meta_refs.resolve_callable(
                    token,
                    conversation_scope_kind=epoch.conversation_scope_kind,
                    scope_subagent_task_id=epoch.scope_subagent_task_id,
                    continuity_epoch_nonce=epoch.epoch_nonce,
                )
                executor, _generation = self._resolve_meta_ref_executor(
                    ref=ref, surface_borrow=surface_borrow
                )
            except LookupError as exc:
                return reject(
                    KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                    "mcp-meta:ref-unavailable",
                    str(exc),
                )
            resolved_name = executor.semantic.provider_tool_name
            frozen_inner = freeze_json(dict(inner))
            if not isinstance(frozen_inner, FrozenJsonObjectFact):
                raise TypeError("MCP meta arguments did not freeze to an object")
            schema = thaw_json(executor.semantic.input_schema)
            if not isinstance(schema, dict):
                raise RuntimeError("MCP meta schema did not thaw to an object")
            try:
                validator = validators.validator_for(schema)
                validator.check_schema(schema)
                validator(schema).validate(dict(inner))
            except ValidationError:
                return reject(
                    KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                    f"mcp-meta:{executor.semantic.descriptor_fingerprint}",
                    "invalid MCP tool arguments",
                    post_name=resolved_name,
                    post_external_name=resolved_name,
                    post_pulsara_name=tool_name,
                    post_arguments=frozen_inner,
                )
            return PreparedResolvedToolInvocation(
                requested_tool_name=tool_name,
                canonical_tool_name=resolved_name,
                external_tool_name=resolved_name,
                pulsara_tool_name=tool_name,
                resolved_arguments=frozen_inner,
            )
        if isinstance(binding.execution_policy, McpToolExecutionPolicyFact):
            semantic = next(
                item
                for item in surface_borrow.prepared.model_surface.tool_specs
                if item.name == binding.tool_name
            )
            schema = thaw_json(semantic.parameters)
            if not isinstance(schema, dict):
                raise RuntimeError("MCP schema did not thaw to an object")
            try:
                validator = validators.validator_for(schema)
                validator.check_schema(schema)
                validator(schema).validate(dict(arguments))
            except ValidationError:
                return reject(
                    KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                    f"mcp-descriptor:{binding.descriptor_fingerprint}",
                    "invalid MCP tool arguments",
                )
        return PreparedResolvedToolInvocation(
            requested_tool_name=tool_name,
            canonical_tool_name=tool_name,
            external_tool_name=tool_name,
            pulsara_tool_name=None,
            resolved_arguments=frozen_original,
        )

    async def freeze_compaction_runtime_handoff(
        self,
        *,
        conversation_scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        maximum_utf8_bytes: int,
    ) -> FrozenCompactionRuntimeHandoff | None:
        """Compose exact bounded views frozen by the existing live owners."""

        if (conversation_scope_kind is ModelInputScopeKind.ROOT) != (
            scope_subagent_task_id is None
        ):
            raise ValueError("runtime handoff scope union is invalid")
        root = self._terminal.workspace_root
        process_facts = tuple(
            FrozenTerminalProcessHandoffFact(
                process_id=item.process_id,
                status="running",
                command_preview=bounded_handoff_preview(item.command),
                cwd=_workspace_relative_handoff_path(Path(item.cwd), root),
            )
            for item in self._terminal.freeze_compaction_handoff(
                owner_host_session_id=self._host_owner_id
            )
            if (
                item.origin.conversation_scope_kind
                == conversation_scope_kind.value
                and item.origin.scope_subagent_task_id == scope_subagent_task_id
            )
        )
        monitor_facts = tuple(
            FrozenTerminalMonitorHandoffFact(
                monitor_id=str(item["monitor_id"]),
                process_id=str(item["process_id"]),
                state=str(item["state"]),
                pending_observation=bool(item["pending_observation"]),
            )
            for item in (
                self._terminal_monitor.freeze_compaction_handoff()
                if conversation_scope_kind is ModelInputScopeKind.ROOT
                else ()
            )
        )
        todo = self._todo_owner.freeze_compaction_handoff(
            scope_kind=conversation_scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
        )
        subagent_facts = ()
        subagent_totals = (
            ("ACTIVE", 0),
            ("PENDING_START", 0),
            ("WAITING_DEPENDENCY", 0),
        )
        if (
            conversation_scope_kind is ModelInputScopeKind.ROOT
            and self._subagent is not None
        ):
            subagent_facts, subagent_totals = (
                await self._subagent.freeze_compaction_handoff()
            )
        return freeze_compaction_runtime_handoff(
            terminal_processes=process_facts,
            terminal_monitors=monitor_facts,
            todo=todo,
            subagent_tasks=subagent_facts,
            subagent_task_totals=subagent_totals,
            maximum_utf8_bytes=maximum_utf8_bytes,
        )

    def prepare_planned_tool_surface(
        self,
        *,
        plan: FrozenToolCapabilityExposurePlan,
        builtin: SealedBuiltinCapabilitySnapshot,
    ) -> PreparedKernelToolSurface:
        """Exact-join one semantic plan to this Host's physical owners."""

        surface = plan.direct_tool_surface
        scope = surface.conversation_scope_kind
        task_id = builtin.scope_subagent_task_id
        if (
            builtin.conversation_scope_kind is not scope
            or plan.direct_projection_set.scope_subagent_task_id != task_id
            or plan.dispatch_view.parent_dispatch_cut.conversation_scope_kind
            is not scope
            or plan.dispatch_view.parent_dispatch_cut.scope_subagent_task_id
            != task_id
        ):
            raise ValueError("planned tool surface scope is invalid")
        with self._surface_lock:
            if self._closed:
                raise RuntimeError("tool surface is closed")
            if (
                self._builtin_composition_state is not BuiltinCompositionState.SEALED
                or builtin.builtin_composition_seal
                is not self._builtin_composition_seal
            ):
                raise RuntimeError("foreign or unsealed builtin snapshot")
            builtin_by_name = {
                item.tool_name: item for item in builtin.executor_bindings
            }
            current_mcp = self._mcp_current
            mcp_binding_by_name = (
                {}
                if current_mcp is None
                else {
                    item.tool_name: item
                    for item in current_mcp.execution_bindings
                }
            )
            mcp_executor_by_name = (
                {} if current_mcp is None else current_mcp.executors
            )
            version_by_name = {
                item.provider_name: item
                for item in plan.direct_projection_set.tool_versions
            }
            leaves: list[DirectToolAccessLeaf] = []
            for spec in surface.tool_specs:
                builtin_binding = builtin_by_name.get(spec.name)
                if builtin_binding is not None:
                    entry = builtin_binding.catalog_entry
                    if entry.descriptor.fingerprint() != spec.descriptor_fingerprint:
                        raise RuntimeError("planned builtin descriptor drifted")
                    policy = BuiltinExecutionPolicyRef(
                        tool_name=spec.name,
                        catalog_entry_fingerprint=entry.entry_fingerprint,
                    )
                    leaves.append(
                        PreparedToolExecutionBinding(
                            tool_name=spec.name,
                            descriptor_fingerprint=spec.descriptor_fingerprint,
                            executor_binding_fingerprint=(
                                production_builtin_executor_binding_identity_fingerprint(
                                    builtin_binding
                                )
                            ),
                            execution_policy=policy,
                        )
                    )
                    continue
                version = version_by_name.get(spec.name)
                mcp_binding = mcp_binding_by_name.get(spec.name)
                mcp_executor = mcp_executor_by_name.get(spec.name)
                if (
                    version is not None
                    and mcp_binding is not None
                    and mcp_executor is not None
                    and mcp_binding.descriptor_fingerprint
                    == spec.descriptor_fingerprint
                    and _mcp_executor_capability_version(mcp_executor) == version
                    and mcp_executor.policy is mcp_binding.execution_policy
                ):
                    leaves.append(mcp_binding)
                    continue
                if version is None:
                    raise RuntimeError("planned MCP version is absent")
                reason = (
                    "SCHEMA_REPLACED_PENDING_COLD_ADOPTION"
                    if mcp_binding is not None
                    else "MCP_DIRECT_CURRENTLY_UNAVAILABLE"
                )
                leaves.append(
                    PreparedUnavailableDirectMcpGate(
                        capability_identity_fingerprint=(
                            version.identity_fingerprint
                        ),
                        tool_semantic_fingerprint=spec.descriptor_fingerprint,
                        provider_tool_name=spec.name,
                        unavailable_reason_code=reason,
                        supervisor_authority_identity=self._mcp_supervisor,
                    )
                )
            frozen_leaves = tuple(leaves)
            access = ProcessLocalToolSurfaceAccess(
                owner_epoch=self._surface_owner_epoch,
                surface_generation=self._surface_generation,
                conversation_scope_kind=scope,
                scope_subagent_task_id=task_id,
                _authority=self._surface_authority,
            )
            prepared = PreparedKernelToolSurface(
                model_surface=surface,
                execution_bindings=frozen_leaves,
                access=access,
                capability_exposure_plan=plan,
            )
            self._prepared_surfaces[(self._surface_generation, scope, task_id)] = (
                prepared
            )
            return prepared

    def borrow_tool_surface(
        self, prepared: PreparedKernelToolSurface
    ) -> ProcessLocalToolSurfaceBorrow:
        with self._surface_lock:
            self._require_prepared_surface_locked(prepared)
            if prepared.access.surface_generation != self._surface_generation:
                raise RuntimeError("retiring tool surface refuses new borrows")
            borrow_id = f"tool-surface-borrow:{uuid4().hex}"
            borrow = ProcessLocalToolSurfaceBorrow(
                prepared=prepared,
                borrow_id=borrow_id,
                _authority=self._surface_authority,
                _validate=self._validate_surface_borrow,
                _release=self._release_surface_borrow,
            )
            self._surface_borrows[borrow_id] = prepared.access.surface_generation
            self._surface_borrow_owners[borrow_id] = borrow
            return borrow

    def validate_tool_surface_borrow(
        self,
        borrow: ProcessLocalToolSurfaceBorrow,
        prepared: PreparedKernelToolSurface,
    ) -> None:
        """Revalidate one pinned surface without selecting an arbitrary tool."""

        with self._surface_lock:
            if (
                borrow._closed
                or borrow._authority is not self._surface_authority
                or borrow.borrow_id not in self._surface_borrows
                or self._surface_borrow_owners.get(borrow.borrow_id) is not borrow
                or not borrow.exactly_joins(prepared)
            ):
                raise RuntimeError("tool surface borrow is not active")
            self._require_prepared_surface_locked(prepared)

    def assert_no_tool_surface_borrows(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
    ) -> None:
        """Prove all process-local surface borrows for this scope are closed."""

        with self._surface_lock:
            if any(
                borrow.prepared.access.conversation_scope_kind is scope_kind
                and borrow.prepared.access.scope_subagent_task_id
                == scope_subagent_task_id
                for borrow in self._surface_borrow_owners.values()
            ):
                raise RuntimeError("no-continuation left a tool surface borrow active")

    def issue_capability_dispatch_observation(
        self,
        *,
        capability_dispatch_cut: FrozenCapabilityDispatchCut,
        prepared_surface: PreparedKernelToolSurface,
        surface_borrow: ProcessLocalToolSurfaceBorrow,
    ) -> object:
        """Issue an exact one-shot carrier only for a registry-current borrow."""

        from pulsara_agent.conversation_kernel.tool_surface import (
            _issue_capability_dispatch_observation,
        )

        with self._surface_lock:
            if (
                surface_borrow._closed
                or surface_borrow._authority is not self._surface_authority
                or surface_borrow.borrow_id not in self._surface_borrows
                or self._surface_borrow_owners.get(surface_borrow.borrow_id)
                is not surface_borrow
                or not surface_borrow.exactly_joins(prepared_surface)
            ):
                raise RuntimeError("tool surface borrow is not active")
            self._require_prepared_surface_locked(prepared_surface)
            access = prepared_surface.access
            if (
                access.conversation_scope_kind
                is not capability_dispatch_cut.conversation_scope_kind
                or access.scope_subagent_task_id
                != capability_dispatch_cut.scope_subagent_task_id
            ):
                raise RuntimeError(
                    "capability dispatch observation does not exact-join"
                )
            return _issue_capability_dispatch_observation(
                capability_dispatch_cut=capability_dispatch_cut,
                prepared_surface=prepared_surface,
                surface_borrow=surface_borrow,
                owner=self,
            )

    def install_provider_input_tool_result_deliveries(
        self,
        *,
        permit: ProcessLocalProviderInputInstallPermit,
        canonical_facts: FrozenCanonicalCompileSnapshot,
        compiled_input: FrozenCompiledModelInput,
        surface_borrow: ProcessLocalToolSurfaceBorrow,
    ) -> None:
        """Install process-local capabilities only after the exact continuity CAS.

        The canonical inspect result may already be durable while its opaque ref
        is still dormant.  Only a FULL compiler decision installed into this
        exact scope/epoch makes the ref callable.  This method owns no database
        mutation and cannot select a different tool generation.
        """

        access = surface_borrow.prepared.access
        if (
            permit.scope.session_id != self._session_id
            or permit.scope.scope_kind is not access.conversation_scope_kind
            or permit.scope.scope_subagent_task_id
            != access.scope_subagent_task_id
        ):
            raise RuntimeError("provider-input install scope does not join tool borrow")
        self.validate_tool_surface_borrow(surface_borrow, surface_borrow.prepared)
        canonical_by_source = {
            compiled_tool_result_source_fingerprint(item): item
            for item in canonical_facts.canonical_input.items
            if item.source_entry_id is not None and item.tool_call_id is not None
        }
        decision_by_source = {
            item.source_entry_fingerprint: item
            for item in compiled_input.tool_result_decisions
        }
        if len(decision_by_source) != len(compiled_input.tool_result_decisions):
            raise RuntimeError("compiled ToolResult delivery decisions are duplicated")
        full_inspect_entry_ids: list[str] = []
        for source_fingerprint, decision in decision_by_source.items():
            if (
                decision.full_delivery_reason
                is not ToolResultFullDeliveryReason.MCP_INSPECT_SCHEMA
            ):
                continue
            item = canonical_by_source.get(source_fingerprint)
            if (
                item is None
                or item.tool_result_delivery.reason
                is not ToolResultFullDeliveryReason.MCP_INSPECT_SCHEMA
                or decision.selected_mode is not ToolResultProviderRenderMode.FULL
            ):
                raise RuntimeError("MCP inspect result was not installed as exact FULL")
            assert item.source_entry_id is not None
            full_inspect_entry_ids.append(item.source_entry_id)
        epoch = _InstalledBorrowEpoch(
            conversation_scope_kind=access.conversation_scope_kind,
            scope_subagent_task_id=access.scope_subagent_task_id,
            epoch_nonce=permit.epoch_nonce,
            epoch_revision=permit.epoch_revision,
        )
        with self._surface_lock:
            if surface_borrow.borrow_id not in self._surface_borrows:
                raise RuntimeError("provider-input install borrow was released")
            self._installed_epoch_by_borrow[surface_borrow.borrow_id] = epoch
        self._mcp_meta_refs.retire_scope_except_epoch(
            conversation_scope_kind=epoch.conversation_scope_kind,
            scope_subagent_task_id=epoch.scope_subagent_task_id,
            continuity_epoch_nonce=epoch.epoch_nonce,
        )
        for entry_id in full_inspect_entry_ids:
            self._mcp_meta_refs.install_full_result(
                result_entry_id=entry_id,
                conversation_scope_kind=epoch.conversation_scope_kind,
                scope_subagent_task_id=epoch.scope_subagent_task_id,
                continuity_epoch_nonce=epoch.epoch_nonce,
            )

    def _require_prepared_surface_locked(
        self, prepared: PreparedKernelToolSurface
    ) -> None:
        access = prepared.access
        key = (
            access.surface_generation,
            access.conversation_scope_kind,
            access.scope_subagent_task_id,
        )
        retained = self._prepared_surfaces.get(key)
        if (
            self._closed
            or access._authority is not self._surface_authority
            or access.owner_epoch != self._surface_owner_epoch
            or retained is None
            or retained is not prepared
        ):
            raise RuntimeError("prepared tool surface is revoked")

    def _validate_surface_borrow(
        self, borrow: ProcessLocalToolSurfaceBorrow, tool_name: str
    ) -> DirectToolAccessLeaf:
        with self._surface_lock:
            if (
                borrow._closed
                or borrow._authority is not self._surface_authority
                or borrow.borrow_id not in self._surface_borrows
                or self._surface_borrow_owners.get(borrow.borrow_id) is not borrow
            ):
                raise RuntimeError("tool surface borrow is not active")
            self._require_prepared_surface_locked(borrow.prepared)
            for binding in borrow.prepared.execution_bindings:
                if binding.tool_name == tool_name:
                    return binding
        raise RuntimeError("tool was not advertised by the prepared surface")

    def _release_surface_borrow(self, borrow: ProcessLocalToolSurfaceBorrow) -> None:
        with self._surface_condition:
            if borrow._authority is not self._surface_authority:
                raise RuntimeError("tool surface borrow authority conflicts")
            if self._surface_borrow_owners.get(borrow.borrow_id) is not borrow:
                raise RuntimeError("tool surface borrow is not registry-current")
            generation = self._surface_borrows.pop(borrow.borrow_id, None)
            self._surface_borrow_owners.pop(borrow.borrow_id, None)
            self._installed_epoch_by_borrow.pop(borrow.borrow_id, None)
            if generation is not None and generation != self._surface_generation:
                if generation not in self._surface_borrows.values():
                    for key in tuple(self._prepared_surfaces):
                        if key[0] == generation:
                            self._prepared_surfaces.pop(key, None)
                    runtime = self._mcp_runtime_by_surface_generation.pop(
                        generation, None
                    )
                    if runtime is not None:
                        runtime.release()
            self._surface_condition.notify_all()

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
    ) -> KernelToolAuthorization:
        try:
            binding = self._validate_surface_borrow(surface_borrow, tool_name)
        except RuntimeError:
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                "tool-surface:revoked",
                f"tool unavailable: {tool_name}",
            )
        if isinstance(binding, PreparedUnavailableDirectMcpGate):
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                f"mcp-direct:{binding.unavailable_reason_code}",
                (
                    "This MCP tool was available when the current context began, "
                    "but its server connection is currently unavailable. The native "
                    "tool definition remains frozen to preserve context continuity. "
                    "Do not route it through use_new_mcp_tool. Check list_mcp_servers "
                    "or wait for a same-schema reconnect."
                ),
            )
        if isinstance(binding.execution_policy, McpToolExecutionPolicyFact):
            return self._authorize_mcp(
                binding=binding,
                arguments=arguments,
                tool_call_id=tool_call_id,
                turn_id=turn_id,
                assistant_entry_id=assistant_entry_id,
                permission_snapshot=permission_snapshot,
                surface_borrow=surface_borrow,
            )
        tool = self._tools.get(tool_name)
        subagent = self._subagent is not None and tool_name in self._subagent.tool_names
        memory = self._memory is not None and tool_name in self._memory.tool_names
        if (tool is None and not subagent and not memory) or self._closed:
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                "tool-surface:unavailable",
                f"tool unavailable: {tool_name}",
            )
        if memory and (
            (
                tool_name in {"remember", "mark_memory_relation"}
                and not memory_context.memory_use_policy.allows_writes
            )
            or (
                tool_name not in {"remember", "mark_memory_relation"}
                and not memory_context.memory_use_policy.allows_reads
            )
        ):
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.PERMISSION_DENIED,
                context_fingerprint(
                    "pulsara:memory-user-opt-out-authorization:v1",
                    {
                        "policy": memory_context.memory_use_policy.value,
                        "tool_name": tool_name,
                    },
                ),
                "memory use was disabled by the user for this run",
            )
        entry = builtin_tool_catalog_entry(tool_name)
        schema = _json_schema_value(entry.descriptor.input_schema)
        try:
            validator = validators.validator_for(schema)
            validator.check_schema(schema)
            validator(schema).validate(dict(arguments))
        except ValidationError as exc:
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                f"descriptor:{entry.descriptor.id}",
                f"invalid tool arguments: {exc.message}",
            )
        if tool_name == "view_image":
            try:
                parse_view_image_source(arguments)
            except ValueError as exc:
                return KernelToolAuthorization(
                    KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                    f"descriptor:{entry.descriptor.id}",
                    f"invalid tool arguments: {exc}",
                )
        if tool_name == "visualization_render":
            try:
                parse_visualization_source(dict(arguments))
            except ValueError as exc:
                return KernelToolAuthorization(
                    KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                    f"descriptor:{entry.descriptor.id}",
                    f"invalid tool arguments: {exc}",
                )
        if tool_name in {"reload_hooks", "reload_capabilities"}:
            access = surface_borrow.prepared.access
            if (
                access.conversation_scope_kind is not ModelInputScopeKind.ROOT
                or access.scope_subagent_task_id is not None
                or permission_snapshot.effective_mode
                is not PermissionMode.BYPASS_PERMISSIONS
            ):
                return KernelToolAuthorization(
                    KernelToolAuthorizationKind.PERMISSION_DENIED,
                    f"{tool_name}_requires_root_bypass_mode",
                    f"{tool_name} requires ROOT bypass-permissions mode",
                )
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.ALLOW,
                f"descriptor:{entry.descriptor.id}:{entry.entry_fingerprint}",
            )
        if subagent:
            access = surface_borrow.prepared.access
            root_tools = {
                "spawn_agent",
                "create_agent_tasks",
                "list_agents",
                "wait_agent",
                "send_agent_message",
                "stop_agent",
            }
            if tool_name in root_tools and (
                access.conversation_scope_kind is not ModelInputScopeKind.ROOT
                or permission_snapshot.effective_mode
                is not PermissionMode.BYPASS_PERMISSIONS
            ):
                return KernelToolAuthorization(
                    KernelToolAuthorizationKind.PERMISSION_DENIED,
                    "subagent_requires_bypass_mode",
                    "ROOT subagent orchestration requires bypass-permissions mode",
                )
            if tool_name == "report_agent_result" and (
                access.conversation_scope_kind is not ModelInputScopeKind.SUBAGENT_TASK
                or access.scope_subagent_task_id is None
            ):
                return KernelToolAuthorization(
                    KernelToolAuthorizationKind.PERMISSION_DENIED,
                    "subagent_report_scope_invalid",
                    "report_agent_result is available only to its worker task",
                )
            argument_error = self._subagent.validate_arguments(
                tool_name=tool_name,
                arguments=arguments,
            )
            if argument_error is not None:
                return KernelToolAuthorization(
                    KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                    f"descriptor:{entry.descriptor.id}",
                    argument_error,
                )
        if tool_name == "todo":
            try:
                parse_todo_replacement(arguments)
            except TodoValidationError as exc:
                return KernelToolAuthorization(
                    KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                    f"descriptor:{entry.descriptor.id}",
                    str(exc),
                )
            access = surface_borrow.prepared.access
            try:
                self._todo_owner.require_active(
                    scope_kind=access.conversation_scope_kind,
                    scope_subagent_task_id=access.scope_subagent_task_id,
                    exact_turn_id=turn_id,
                )
            except LookupError:
                return KernelToolAuthorization(
                    KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                    "todo-run:inactive",
                    "todo scope is no longer active",
                )
        if tool_name == "use_new_mcp_tool":
            return self._authorize_meta_mcp(
                arguments=arguments,
                tool_call_id=tool_call_id,
                turn_id=turn_id,
                permission_snapshot=permission_snapshot,
                surface_borrow=surface_borrow,
            )
        capability_call = None
        if tool_name == "manage_capability":
            access = surface_borrow.prepared.access
            if access.conversation_scope_kind is not ModelInputScopeKind.ROOT:
                return KernelToolAuthorization(KernelToolAuthorizationKind.PERMISSION_DENIED,
                    "capability:root-only", "Capability management is available only in ROOT")
            if self._capability_management is None:
                return KernelToolAuthorization(KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                    "capability:unavailable", "Capability management is unavailable")
            try:
                prepared = await self._capability_management.prepare(arguments)
            except ValueError as exc:
                return KernelToolAuthorization(KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                    "capability:invalid-target", str(exc))
            capability_call = CapabilityManagementCall(self._capability_management, prepared)
            capability_call.subject = (self._session_id, turn_id, assistant_entry_id, tool_call_id)
        decision = await self._authorization_policy.decide(
            ToolDispatchAuthorizationRequest(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                arguments=dict(arguments),
                turn_id=turn_id,
                assistant_entry_id=assistant_entry_id,
                permission_snapshot=permission_snapshot,
                workspace_root=self._workspace_root,
                capability_effects=capability_call.prepared.effects if capability_call else None,
            )
        )
        if capability_call is not None:
            readonly_form = (decision.kind is ToolDispatchDecisionKind.DENY
                             and permission_snapshot.effective_mode is PermissionMode.READ_ONLY)
            if decision.kind is ToolDispatchDecisionKind.DENY and not readonly_form:
                capability_call.discard()
                return KernelToolAuthorization(KernelToolAuthorizationKind.PERMISSION_DENIED,
                    decision.reference, decision.public_message)
            form = readonly_form or bool(capability_call.prepared.user_inputs) or (
                decision.kind is ToolDispatchDecisionKind.REQUIRE_CONFIRMATION)
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.CAPABILITY_FORM_REQUIRED if form else KernelToolAuthorizationKind.ALLOW,
                decision.reference, decision.public_message,
                capability_call=capability_call,
                capability_permission_required=decision.kind is ToolDispatchDecisionKind.REQUIRE_CONFIRMATION,
            )
        if decision.kind is ToolDispatchDecisionKind.REQUIRE_CONFIRMATION:
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.REQUIRE_CONFIRMATION,
                decision.reference,
                decision.public_message,
            )
        if decision.kind is ToolDispatchDecisionKind.DENY:
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.PERMISSION_DENIED,
                decision.reference,
                decision.public_message,
            )
        if decision.kind is not ToolDispatchDecisionKind.ALLOW:
            raise RuntimeError("permission decision vocabulary is invalid")
        if tool_name in {"read_mcp_resource", "get_mcp_prompt"}:
            generation = surface_borrow.prepared.access.surface_generation
            runtime = self._mcp_runtime_by_surface_generation.get(generation)
            if runtime is None:
                return KernelToolAuthorization(
                    KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                    "mcp-runtime:unavailable",
                    "MCP runtime is unavailable",
                )
            try:
                permit = runtime.admit_standard_operation(
                    tool_name=tool_name,
                    arguments=arguments,
                    descriptor_fingerprint=entry.descriptor.fingerprint(),
                    session_id=self._session_id,
                    scope_kind=(surface_borrow.prepared.access.conversation_scope_kind),
                    scope_subagent_task_id=(
                        surface_borrow.prepared.access.scope_subagent_task_id
                    ),
                    turn_id=turn_id,
                    tool_call_id=tool_call_id,
                )
            except McpSnapshotStale:
                return KernelToolAuthorization(
                    KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                    "mcp-runtime:snapshot-stale",
                    "MCP_SNAPSHOT_STALE",
                )
            except ValueError:
                return KernelToolAuthorization(
                    KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                    "mcp-runtime:catalog-join-invalid",
                    "MCP resource or prompt arguments do not match the exact catalog",
                )
            if permit is None:
                raise RuntimeError("MCP remote read did not create a permit")
            key = (generation, tool_call_id)
            if key in self._mcp_dispatch_permits:
                permit.release()
                raise RuntimeError("MCP standard operation was authorized twice")
            self._mcp_dispatch_permits[key] = permit
        return KernelToolAuthorization(
            KernelToolAuthorizationKind.ALLOW,
            f"descriptor:{entry.descriptor.id}:{entry.entry_fingerprint}",
        )

    def _installed_epoch_for_borrow(
        self, surface_borrow: ProcessLocalToolSurfaceBorrow
    ) -> _InstalledBorrowEpoch:
        with self._surface_lock:
            epoch = self._installed_epoch_by_borrow.get(surface_borrow.borrow_id)
        if epoch is None:
            raise LookupError("MCP_TOOL_REF_PROVIDER_EPOCH_NOT_INSTALLED")
        access = surface_borrow.prepared.access
        if (
            epoch.conversation_scope_kind is not access.conversation_scope_kind
            or epoch.scope_subagent_task_id != access.scope_subagent_task_id
        ):
            raise RuntimeError("installed MCP ref epoch scope drifted")
        return epoch

    def _resolve_meta_ref_executor(
        self,
        *,
        ref: NewMcpToolRef,
        surface_borrow: ProcessLocalToolSurfaceBorrow,
    ) -> tuple[McpBoundToolExecutor, int]:
        plan = surface_borrow.prepared.capability_exposure_plan
        if plan is None:
            raise LookupError("MCP_TOOL_REF_ROUTE_UNAVAILABLE")
        matches = tuple(
            item
            for item in plan.mcp_catalog_route_projection.routes
            if item.route_fingerprint == ref.tool_route_fingerprint
            and item.version.identity_fingerprint
            == ref.capability_identity_fingerprint
            and item.version.semantic_fingerprint == ref.tool_semantic_fingerprint
        )
        if len(matches) != 1:
            raise LookupError("MCP_TOOL_REF_STALE_OR_FOREIGN")
        route = matches[0]
        if route.route is not ToolCapabilityRouteKind.NEW_MCP_META_ONLY:
            raise LookupError("MCP_TOOL_REF_NOT_META_CALLABLE")
        generation = surface_borrow.prepared.access.surface_generation
        runtime = self._mcp_runtime_by_surface_generation.get(generation)
        if runtime is None:
            raise LookupError("MCP_TOOL_REF_RUNTIME_UNAVAILABLE")
        executor = runtime.executors.get(route.version.provider_name)
        if executor is None:
            raise LookupError("MCP_TOOL_REF_EXECUTOR_UNAVAILABLE")
        version = _mcp_executor_capability_version(executor)
        if (
            version != route.version
            or execution_policy_fingerprint(executor.policy)
            != ref.mcp_execution_policy_fingerprint
        ):
            raise LookupError("MCP_TOOL_REF_BINDING_STALE")
        return executor, generation

    def _authorize_meta_mcp(
        self,
        *,
        arguments: Mapping[str, object],
        tool_call_id: str,
        turn_id: str,
        permission_snapshot: FrozenRunPermissionSnapshot,
        surface_borrow: ProcessLocalToolSurfaceBorrow,
    ) -> KernelToolAuthorization:
        token = arguments.get("tool_ref")
        inner = arguments.get("arguments")
        if not isinstance(token, str) or not isinstance(inner, Mapping):
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                "mcp-meta:arguments-invalid",
                "invalid new MCP tool reference or arguments",
            )
        try:
            epoch = self._installed_epoch_for_borrow(surface_borrow)
            ref = self._mcp_meta_refs.resolve_callable(
                token,
                conversation_scope_kind=epoch.conversation_scope_kind,
                scope_subagent_task_id=epoch.scope_subagent_task_id,
                continuity_epoch_nonce=epoch.epoch_nonce,
            )
            executor, generation = self._resolve_meta_ref_executor(
                ref=ref, surface_borrow=surface_borrow
            )
        except LookupError as exc:
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                "mcp-meta:ref-unavailable",
                str(exc),
            )
        schema = thaw_json(executor.semantic.input_schema)
        if not isinstance(schema, dict):
            raise RuntimeError("MCP meta schema did not thaw to an object")
        try:
            validator = validators.validator_for(schema)
            validator.check_schema(schema)
            validator(schema).validate(dict(inner))
        except ValidationError:
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                f"mcp-meta:{executor.semantic.descriptor_fingerprint}",
                "invalid MCP tool arguments",
            )
        mode = permission_snapshot.effective_mode
        if (
            executor.policy.effect_kind is McpEffectKind.EXTERNAL_EFFECT
            and mode is PermissionMode.READ_ONLY
        ):
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.PERMISSION_DENIED,
                f"mcp-policy:{execution_policy_fingerprint(executor.policy)}",
                "external MCP effects are denied in read-only mode",
            )
        key = (generation, tool_call_id)
        if (
            key in self._mcp_dispatch_permits
            or key in self._mcp_confirmation_admissions
            or key in self._mcp_meta_invocations
        ):
            raise RuntimeError("MCP meta tool call was authorized twice")
        frozen_arguments = freeze_json(dict(inner))
        if not isinstance(frozen_arguments, FrozenJsonObjectFact):
            raise TypeError("MCP meta arguments did not freeze to an object")
        prepared = _PreparedMcpMetaInvocation(
            executor=executor,
            ref=ref,
            arguments=frozen_arguments,
        )
        self._mcp_meta_invocations[key] = prepared
        if executor.policy.effect_kind is McpEffectKind.EXTERNAL_EFFECT and mode in {
            PermissionMode.ASK_PERMISSIONS,
            PermissionMode.ACCEPT_EDITS,
        }:
            self._mcp_confirmation_admissions[key] = _PendingMcpConfirmationAdmission(
                generation=generation,
                executor=executor,
                scope_kind=surface_borrow.prepared.access.conversation_scope_kind,
                scope_subagent_task_id=(
                    surface_borrow.prepared.access.scope_subagent_task_id
                ),
                turn_id=turn_id,
                tool_call_id=tool_call_id,
            )
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.REQUIRE_CONFIRMATION,
                f"mcp-policy:{execution_policy_fingerprint(executor.policy)}",
                f"Allow external MCP action {executor.semantic.remote_tool_name}?",
            )
        try:
            permit = executor.admit(
                session_id=self._session_id,
                scope_kind=surface_borrow.prepared.access.conversation_scope_kind,
                scope_subagent_task_id=(
                    surface_borrow.prepared.access.scope_subagent_task_id
                ),
                turn_id=turn_id,
                tool_call_id=tool_call_id,
            )
        except McpSnapshotStale:
            self._mcp_meta_invocations.pop(key, None)
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                "mcp-runtime:snapshot-stale",
                "MCP_SNAPSHOT_STALE",
            )
        self._mcp_dispatch_permits[key] = permit
        return KernelToolAuthorization(
            KernelToolAuthorizationKind.ALLOW,
            f"mcp-policy:{execution_policy_fingerprint(executor.policy)}",
        )

    def _authorize_mcp(
        self,
        *,
        binding: PreparedToolExecutionBinding,
        arguments: Mapping[str, object],
        tool_call_id: str,
        turn_id: str,
        assistant_entry_id: str,
        permission_snapshot: FrozenRunPermissionSnapshot,
        surface_borrow: ProcessLocalToolSurfaceBorrow,
    ) -> KernelToolAuthorization:
        policy = binding.execution_policy
        if not isinstance(policy, McpToolExecutionPolicyFact):
            raise TypeError("dynamic MCP policy union is invalid")
        semantic = next(
            item
            for item in surface_borrow.prepared.model_surface.tool_specs
            if item.name == binding.tool_name
        )
        schema = thaw_json(semantic.parameters)
        if not isinstance(schema, dict):
            raise RuntimeError("MCP schema did not thaw to an object")
        try:
            validator = validators.validator_for(schema)
            validator.check_schema(schema)
            validator(schema).validate(dict(arguments))
        except ValidationError:
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                f"mcp-descriptor:{binding.descriptor_fingerprint}",
                "invalid MCP tool arguments",
            )
        generation = surface_borrow.prepared.access.surface_generation
        runtime = self._mcp_runtime_by_surface_generation.get(generation)
        if runtime is None:
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                "mcp-runtime:retired",
                "MCP tool generation is no longer available",
            )
        executor = runtime.executors.get(binding.tool_name)
        if executor is None:
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                "mcp-runtime:binding-missing",
                "MCP tool binding is unavailable",
            )
        mode = permission_snapshot.effective_mode
        if (
            policy.effect_kind is McpEffectKind.EXTERNAL_EFFECT
            and mode is PermissionMode.READ_ONLY
        ):
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.PERMISSION_DENIED,
                f"mcp-policy:{execution_policy_fingerprint(policy)}",
                "external MCP effects are denied in read-only mode",
            )
        key = (generation, tool_call_id)
        if (
            key in self._mcp_dispatch_permits
            or key in self._mcp_confirmation_admissions
        ):
            raise RuntimeError("MCP tool call was authorized twice")
        if policy.effect_kind is McpEffectKind.EXTERNAL_EFFECT and mode in {
            PermissionMode.ASK_PERMISSIONS,
            PermissionMode.ACCEPT_EDITS,
        }:
            self._mcp_confirmation_admissions[key] = _PendingMcpConfirmationAdmission(
                generation=generation,
                executor=executor,
                scope_kind=(surface_borrow.prepared.access.conversation_scope_kind),
                scope_subagent_task_id=(
                    surface_borrow.prepared.access.scope_subagent_task_id
                ),
                turn_id=turn_id,
                tool_call_id=tool_call_id,
            )
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.REQUIRE_CONFIRMATION,
                f"mcp-policy:{execution_policy_fingerprint(policy)}",
                f"Allow external MCP action {binding.tool_name}?",
            )
        try:
            permit = executor.admit(
                session_id=self._session_id,
                scope_kind=surface_borrow.prepared.access.conversation_scope_kind,
                scope_subagent_task_id=(
                    surface_borrow.prepared.access.scope_subagent_task_id
                ),
                turn_id=turn_id,
                tool_call_id=tool_call_id,
            )
        except McpSnapshotStale:
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                "mcp-runtime:snapshot-stale",
                "MCP_SNAPSHOT_STALE",
            )
        self._mcp_dispatch_permits[key] = permit
        return KernelToolAuthorization(
            KernelToolAuthorizationKind.ALLOW,
            f"mcp-policy:{execution_policy_fingerprint(policy)}",
        )

    def prepare_permission_request(
        self,
        *,
        tool_call_id: str,
        turn_id: str,
        surface_borrow: ProcessLocalToolSurfaceBorrow,
    ) -> PreparedPermissionRequest:
        access = surface_borrow.prepared.access
        state_key = (access.surface_generation, tool_call_id)
        admission = self._mcp_confirmation_admissions.get(state_key)
        permit = self._mcp_dispatch_permits.get(state_key)
        if admission is not None and (
            admission.generation != access.surface_generation
            or admission.turn_id != turn_id
            or admission.scope_kind is not access.conversation_scope_kind
            or admission.scope_subagent_task_id != access.scope_subagent_task_id
        ):
            raise RuntimeError("permission request admission does not exact-join")
        if permit is not None and (
            permit.turn_id != turn_id
            or permit.scope_kind is not access.conversation_scope_kind
            or permit.scope_subagent_task_id != access.scope_subagent_task_id
        ):
            raise RuntimeError("permission request permit does not exact-join")
        if admission is None and permit is None:
            state_key = None
        return PreparedPermissionRequest(
            tool_call_id=tool_call_id,
            turn_id=turn_id,
            scope_kind=access.conversation_scope_kind,
            scope_subagent_task_id=access.scope_subagent_task_id,
            request_nonce=object(),
            pending_state_key=state_key,
            pending_admission=admission,
            pending_permit=permit,
            pending_meta_invocation=(
                None if state_key is None else self._mcp_meta_invocations.get(state_key)
            ),
        )

    def _validate_permission_request(
        self, prepared: PreparedPermissionRequest
    ) -> tuple[
        tuple[int, str] | None,
        _PendingMcpConfirmationAdmission | None,
        McpDispatchAdmissionPermit | None,
    ]:
        key = prepared.pending_state_key
        if key is None:
            return None, None, None
        if not isinstance(key, tuple) or len(key) != 2:
            raise RuntimeError("permission request state key is invalid")
        admission = self._mcp_confirmation_admissions.get(key)
        permit = self._mcp_dispatch_permits.get(key)
        if (
            admission is not prepared.pending_admission
            or permit is not prepared.pending_permit
            or self._mcp_meta_invocations.get(key)
            is not prepared.pending_meta_invocation
        ):
            raise RuntimeError("permission request owner changed")
        return key, admission, permit

    def resolve_hook_permission(
        self, *, prepared_request: PreparedPermissionRequest, allow: bool
    ) -> KernelToolAuthorization:
        key, admission, permit = self._validate_permission_request(
            prepared_request
        )
        if not allow:
            if key is not None and admission is not None:
                self._mcp_confirmation_admissions.pop(key, None)
                self._mcp_meta_invocations.pop(key, None)
            if key is not None and permit is not None:
                self._mcp_dispatch_permits.pop(key, None)
                if permit.state.value == "ADMITTED":
                    permit.release()
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.PERMISSION_DENIED,
                "hook:permission-deny",
                "Hook denied this exact permission request",
            )
        if key is not None and admission is not None:
            if self._mcp_confirmation_admissions.pop(key, None) is not admission:
                raise RuntimeError("MCP Hook permission owner changed")
            try:
                permit = admission.executor.admit(
                    session_id=self._session_id,
                    scope_kind=admission.scope_kind,
                    scope_subagent_task_id=admission.scope_subagent_task_id,
                    turn_id=admission.turn_id,
                    tool_call_id=admission.tool_call_id,
                )
            except BaseException:
                self._mcp_meta_invocations.pop(key, None)
                raise
            if key in self._mcp_dispatch_permits:
                permit.release()
                raise RuntimeError("MCP Hook permission permit already exists")
            self._mcp_dispatch_permits[key] = permit
        return KernelToolAuthorization(
            KernelToolAuthorizationKind.ALLOW,
            "hook:permission-allow",
        )

    async def request_capability_form(
        self, *, authorization, turn_id, assistant_entry_id, tool_call_id, permission_snapshot,
    ):
        call = authorization.capability_call
        if call is None or authorization.kind is not KernelToolAuthorizationKind.CAPABILITY_FORM_REQUIRED:
            raise RuntimeError("capability form has no prepared call")
        if self._interaction is None:
            call.discard()
            return KernelToolAuthorization(KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                "interaction:no-controller", "Capability configuration requires a controller")
        try:
            resolution = await self._interaction.request_capability_form(
                turn_id=turn_id, assistant_entry_id=assistant_entry_id, tool_call_id=tool_call_id,
                permission_snapshot=permission_snapshot, form=call.form(authorization.public_message),
            )
        except BaseException:
            call.discard()
            raise
        if resolution.decision == "SUBMIT" and resolution.capability_submission is not None:
            call.accept(resolution.capability_submission)
            return KernelToolAuthorization(KernelToolAuthorizationKind.ALLOW,
                resolution.reference, resolution.public_message,
                capability_call=call, capability_user_submission=True)
        call.discard()
        return KernelToolAuthorization(
            KernelToolAuthorizationKind.TOOL_UNAVAILABLE if "no-controller" in resolution.reference
            else KernelToolAuthorizationKind.CANCELLED_BEFORE_DISPATCH,
            resolution.reference, resolution.public_message,
        )

    async def request_confirmation(
        self,
        *,
        prepared_request: PreparedPermissionRequest,
        tool_name: str,
        assistant_entry_id: str,
        permission_snapshot: FrozenRunPermissionSnapshot,
    ) -> KernelToolAuthorization:
        state_key, admission, permit = self._validate_permission_request(
            prepared_request
        )
        admission_entry = (
            None if state_key is None or admission is None else (state_key, admission)
        )
        permit_entry = (
            None if state_key is None or permit is None else (state_key, permit)
        )
        tool_call_id = prepared_request.tool_call_id
        turn_id = prepared_request.turn_id
        if self._interaction is None:
            if admission_entry is not None:
                self._mcp_confirmation_admissions.pop(admission_entry[0], None)
                self._mcp_meta_invocations.pop(admission_entry[0], None)
            if permit_entry is not None:
                self._mcp_dispatch_permits.pop(permit_entry[0], None)
                permit_entry[1].release()
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.PERMISSION_DENIED,
                "interaction:no-controller-owner",
                "tool execution requires confirmation but no controller is attached",
            )

        def admit_before_publish() -> None:
            if admission_entry is None:
                return
            key, admission = admission_entry
            if self._mcp_confirmation_admissions.pop(key, None) is not admission:
                raise RuntimeError("MCP confirmation admission owner changed")
            permit = admission.executor.admit(
                session_id=self._session_id,
                scope_kind=admission.scope_kind,
                scope_subagent_task_id=admission.scope_subagent_task_id,
                turn_id=admission.turn_id,
                tool_call_id=admission.tool_call_id,
            )
            if key in self._mcp_dispatch_permits:
                permit.release()
                raise RuntimeError("MCP confirmation permit already exists")
            self._mcp_dispatch_permits[key] = permit

        def discard_admission() -> None:
            if admission_entry is not None:
                self._mcp_confirmation_admissions.pop(admission_entry[0], None)
                self._mcp_meta_invocations.pop(admission_entry[0], None)
                current = self._mcp_dispatch_permits.pop(admission_entry[0], None)
                if current is not None and current.state.value == "ADMITTED":
                    current.release()

        resolution = await self._interaction.request_tool_confirmation(
            turn_id=turn_id,
            assistant_entry_id=assistant_entry_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            permission_snapshot=permission_snapshot,
            admission_hooks=(
                InteractionAdmissionHooks(
                    before_publish=admit_before_publish,
                    discard=discard_admission,
                    owner_key=(
                        "mcp-server:" + admission_entry[1].executor.semantic.server_id
                    ),
                )
                if admission_entry is not None
                else None
            ),
        )
        permit_entry = (
            None
            if state_key is None
            else (
                (state_key, current)
                if (current := self._mcp_dispatch_permits.get(state_key)) is not None
                else None
            )
        )
        if resolution.decision == "ALLOW":
            if permit_entry is not None:
                permit_entry[1].mark_attempt_accepted()
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.ALLOW,
                resolution.reference,
                resolution.public_message,
                accepted_attempt_id=resolution.attempt_id,
                accepted_permission_snapshot_fingerprint=(
                    resolution.permission_snapshot_fingerprint
                ),
            )
        if resolution.decision == "DENY":
            if permit_entry is not None:
                self._mcp_dispatch_permits.pop(permit_entry[0], None)
                self._mcp_meta_invocations.pop(permit_entry[0], None)
                if permit_entry[1].state.value == "ADMITTED":
                    permit_entry[1].release()
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.PERMISSION_DENIED,
                resolution.reference,
                resolution.public_message,
                accepted_result_entry_id=resolution.result_entry_id,
                accepted_result_id=resolution.result_id,
                accepted_result_entry_sequence=(
                    resolution.result_entry_sequence
                ),
                accepted_result_observed_at=resolution.result_observed_at,
                accepted_result_public_body=resolution.result_public_body,
            )
        raise RuntimeError("interaction resolution vocabulary is invalid")

    def _list_mcp_servers_result(
        self,
        *,
        arguments: Mapping[str, object],
        surface_borrow: ProcessLocalToolSurfaceBorrow,
    ) -> KernelToolResult:
        generation = surface_borrow.prepared.access.surface_generation
        runtime = self._mcp_runtime_by_surface_generation.get(generation)
        plan = surface_borrow.prepared.capability_exposure_plan
        if runtime is None or plan is None:
            return _local_mcp_application_error("MCP_CATALOG_UNAVAILABLE")
        scope_kind = surface_borrow.prepared.access.conversation_scope_kind
        catalog = runtime.catalog_for_scope(scope_kind)
        if (
            catalog.semantic_fingerprint
            != plan.mcp_catalog_route_projection.joined_catalog_semantic_fingerprint
        ):
            return _local_mcp_application_error("MCP_CATALOG_STALE")
        page = self._mcp_directory.render(
            arguments=arguments,
            scope_kind=scope_kind,
            scope_subagent_task_id=(
                surface_borrow.prepared.access.scope_subagent_task_id
            ),
            catalog=catalog,
            candidates=runtime.candidates,
            routes=plan.mcp_catalog_route_projection,
            direct_projection_set=plan.direct_projection_set,
        )
        return KernelToolResult(
            state=page.state,
            content=page.content,
            effect_class="read_only",
        )

    def _list_mcp_items_result(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        surface_borrow: ProcessLocalToolSurfaceBorrow,
    ) -> KernelToolResult:
        generation = surface_borrow.prepared.access.surface_generation
        runtime = self._mcp_runtime_by_surface_generation.get(generation)
        if runtime is None:
            return _local_mcp_application_error("MCP_CATALOG_UNAVAILABLE")
        scope_kind = surface_borrow.prepared.access.conversation_scope_kind
        page = self._mcp_directory.render_items(
            tool_name=tool_name,
            arguments=arguments,
            scope_kind=scope_kind,
            scope_subagent_task_id=(
                surface_borrow.prepared.access.scope_subagent_task_id
            ),
            catalog=runtime.catalog_for_scope(scope_kind),
            candidates=runtime.candidates,
        )
        return KernelToolResult(
            state=page.state,
            content=page.content,
            effect_class="read_only",
        )

    def _inspect_new_mcp_tool_result(
        self,
        *,
        arguments: Mapping[str, object],
        invocation_context: KernelToolInvocationContext,
    ) -> KernelToolResult:
        server_id = arguments.get("server_id")
        remote_name = arguments.get("tool_name")
        if not isinstance(server_id, str) or not isinstance(remote_name, str):
            return _local_mcp_application_error("INVALID_ARGUMENTS")
        borrow = invocation_context.surface_borrow
        plan = borrow.prepared.capability_exposure_plan
        if plan is None:
            return _local_mcp_application_error("MCP_CATALOG_UNAVAILABLE")
        matches = tuple(
            item
            for item in plan.mcp_catalog_route_projection.routes
            if item.target.server_id == server_id
            and remote_name
            in {
                item.target.remote_tool_name,
                item.version.provider_name,
            }
        )
        if len(matches) != 1:
            return _local_mcp_application_error("MCP_TOOL_NOT_FOUND")
        route = matches[0]
        if route.route is ToolCapabilityRouteKind.DIRECT:
            return _local_mcp_application_error("MCP_TOOL_IS_NATIVE_DIRECT")
        if route.route is ToolCapabilityRouteKind.UNAVAILABLE:
            return _local_mcp_application_error(route.public_reason_code.value)
        generation = borrow.prepared.access.surface_generation
        runtime = self._mcp_runtime_by_surface_generation.get(generation)
        if runtime is None:
            return _local_mcp_application_error("MCP_RUNTIME_UNAVAILABLE")
        executor = runtime.executors.get(route.version.provider_name)
        if executor is None or _mcp_executor_capability_version(executor) != route.version:
            return _local_mcp_application_error("MCP_TOOL_BINDING_STALE")
        values = _mcp_inspection_values(executor)
        quote = conservative_mcp_inspection_logical_utf8_bytes(values)
        if quote > MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES:
            return _local_mcp_application_error("MCP_DESCRIPTOR_OVERBOUND")
        epoch = self._installed_epoch_for_borrow(borrow)
        try:
            prepared = self._mcp_meta_refs.prepare(
                conversation_scope_kind=epoch.conversation_scope_kind,
                scope_subagent_task_id=epoch.scope_subagent_task_id,
                continuity_epoch_nonce=epoch.epoch_nonce,
                capability_identity_fingerprint=(
                    route.version.identity_fingerprint
                ),
                tool_semantic_fingerprint=route.version.semantic_fingerprint,
                mcp_execution_policy_fingerprint=(
                    execution_policy_fingerprint(executor.policy)
                ),
                tool_route_fingerprint=route.route_fingerprint,
                result_entry_id=invocation_context.result_entry_id,
            )
        except McpToolRefCapacityExceeded:
            return _local_mcp_application_error("MCP_REF_CAPACITY_EXCEEDED")
        try:
            body = render_inspected_new_mcp_tool_provider_result(
                values,
                tool_ref=prepared.ref.opaque_token,
            ).encode("utf-8")
        except BaseException:
            self._mcp_meta_refs.settle(
                prepared=prepared,
                committed=False,
            )
            raise
        token = ProcessLocalEffectSettlementToken(
            prepared.settlement_token_id,
            prepared,
        )
        self._mcp_ref_settlements[token.token_id] = token
        return KernelToolResult(
            state="SUCCESS",
            content=body,
            process_local_settlement=token,
            effect_class="read_only",
        )

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
    ) -> KernelToolResult:
        if self._closed:
            raise RuntimeError("tool surface is closed")
        if (
            invocation_context.session_id != self._session_id
            or invocation_context.turn_id != turn_id
            or invocation_context.assistant_entry_id != assistant_entry_id
            or invocation_context.tool_call_id != tool_call_id
            or invocation_context.attempt_id != attempt_id
            or invocation_context.attempt_permission_snapshot_fingerprint
            != invocation_context.permission_snapshot_fingerprint
        ):
            # This check precedes every adapter dispatch.  A mismatched
            # process-local authority must never create a monitor, process or
            # any other physical effect before being rejected.
            raise RuntimeError("tool invocation context does not exact-join request")
        if (
            invocation_context.conversation_scope_kind
            != invocation_context.surface_borrow.prepared.access.conversation_scope_kind.value
            or invocation_context.scope_subagent_task_id
            != invocation_context.surface_borrow.prepared.access.scope_subagent_task_id
        ):
            raise RuntimeError("tool invocation surface binding does not exact-join")
        self._validate_surface_borrow(invocation_context.surface_borrow, tool_name)
        binding = invocation_context.surface_borrow.execution_binding(tool_name)
        if isinstance(binding, PreparedUnavailableDirectMcpGate):
            raise RuntimeError("unavailable MCP gate cannot invoke a physical tool")
        invocation_started = monotonic()
        observation_origin = tool_observation_origin_for_binding(binding)
        if tool_name == "manage_capability":
            call = invocation_context.capability_call
            if (call is None or call.service is not self._capability_management
                or call.subject != (self._session_id, turn_id, assistant_entry_id, tool_call_id)
                or invocation_context.conversation_scope_kind != "ROOT"):
                raise RuntimeError("capability execution lost its exact prepared owner")
            try:
                values = await call.execute()
            except McpManagementConflict as exc:
                values = {"status": "CONFLICT", "message": str(exc), "adoption": "NOT_APPLICABLE"}
            except ValueError:
                # Private form values may be present in a native validator's
                # exception. Do not put that exception into tool/Hook context.
                values = {"status": "REJECTED", "message": "Capability operation could not be applied; review its current configuration.", "adoption": "NOT_APPLICABLE"}
            if values["status"] == "APPLIED":
                try:
                    values["adoption"] = await self._capability_reload.adopt_capability_management_change(
                        workspace_root=call._root())
                except asyncio.CancelledError:
                    # Source is already settled. Preserve that fact; a later
                    # safe point may retry adoption, never replay the mutation.
                    values["adoption"] = "PARTIAL"
                except Exception:
                    values["adoption"] = "PARTIAL"
            return KernelToolResult(
                state="SUCCESS" if values["status"] == "APPLIED" else "APPLICATION_ERROR",
                content=json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode(),
                effect_class="unknown_effect",
                physical_observation=_freeze_physical_observation(invocation_started, observation_origin),
            )
        if tool_name == "reload_hooks":
            if self._capability_reload is None:
                raise RuntimeError("Hook reload port is unavailable")
            values = await self._capability_reload.reload_hooks(
                deadline_monotonic=self._deadlines.deadline(
                    KernelWatchdogOwner.NONTERMINAL_TOOL_INVOCATION
                )
            )
            safe_values = HookSecretScrubSet.capture().scrub_json(dict(values))
            if not isinstance(safe_values, dict):
                raise RuntimeError("Hook reload result lost its JSON object shape")
            content = json.dumps(
                safe_values,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            return KernelToolResult(
                state="SUCCESS",
                content=content,
                effect_class="read_only",
                physical_observation=_freeze_physical_observation(
                    invocation_started, observation_origin
                ),
            )
        if tool_name == "reload_capabilities":
            if self._capability_reload is None:
                raise RuntimeError("Capability reload port is unavailable")
            values = await self._capability_reload.reload_capabilities(
                deadline_monotonic=self._deadlines.deadline(
                    KernelWatchdogOwner.NONTERMINAL_TOOL_INVOCATION
                )
            )
            safe_values = HookSecretScrubSet.capture().scrub_json(dict(values))
            if not isinstance(safe_values, dict):
                raise RuntimeError("Capability reload result lost its JSON object shape")
            content = json.dumps(
                safe_values,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            return KernelToolResult(
                state="SUCCESS",
                content=content,
                effect_class="read_only",
                physical_observation=_freeze_physical_observation(
                    invocation_started, observation_origin
                ),
            )
        if tool_name == "list_mcp_servers":
            return replace(
                self._list_mcp_servers_result(
                    arguments=arguments,
                    surface_borrow=invocation_context.surface_borrow,
                ),
                physical_observation=_freeze_physical_observation(
                    invocation_started, observation_origin
                ),
            )
        if tool_name in {
            "list_mcp_prompts",
            "list_mcp_resource_templates",
            "list_mcp_resources",
        }:
            return replace(
                self._list_mcp_items_result(
                    tool_name=tool_name,
                    arguments=arguments,
                    surface_borrow=invocation_context.surface_borrow,
                ),
                physical_observation=_freeze_physical_observation(
                    invocation_started, observation_origin
                ),
            )
        if tool_name == "inspect_new_mcp_tool":
            return replace(
                self._inspect_new_mcp_tool_result(
                    arguments=arguments,
                    invocation_context=invocation_context,
                ),
                physical_observation=_freeze_physical_observation(
                    invocation_started, observation_origin
                ),
            )
        if tool_name == "use_new_mcp_tool":
            generation = (
                invocation_context.surface_borrow.prepared.access.surface_generation
            )
            key = (generation, tool_call_id)
            prepared_meta = self._mcp_meta_invocations.pop(key, None)
            permit = self._mcp_dispatch_permits.pop(key, None)
            if prepared_meta is None or permit is None:
                if permit is not None and permit.state.value == "ADMITTED":
                    permit.release()
                raise RuntimeError("MCP meta invocation admission is incomplete")
            token = arguments.get("tool_ref")
            inner = arguments.get("arguments")
            frozen_inner = freeze_json(dict(inner)) if isinstance(inner, Mapping) else None
            try:
                epoch = self._installed_epoch_for_borrow(
                    invocation_context.surface_borrow
                )
                resolved_ref = self._mcp_meta_refs.resolve_callable(
                    str(token),
                    conversation_scope_kind=epoch.conversation_scope_kind,
                    scope_subagent_task_id=epoch.scope_subagent_task_id,
                    continuity_epoch_nonce=epoch.epoch_nonce,
                )
                resolved_executor, resolved_generation = (
                    self._resolve_meta_ref_executor(
                        ref=resolved_ref,
                        surface_borrow=invocation_context.surface_borrow,
                    )
                )
                if (
                    resolved_generation != generation
                    or resolved_ref != prepared_meta.ref
                    or resolved_executor != prepared_meta.executor
                    or frozen_inner != prepared_meta.arguments
                ):
                    raise RuntimeError("MCP meta invocation changed after authorization")
            except BaseException:
                if permit.state.value == "ADMITTED":
                    permit.release()
                raise
            if permit.state.value == "ADMITTED":
                permit.mark_attempt_accepted()
            operation_task = asyncio.create_task(
                prepared_meta.executor.invoke(
                    permit, thaw_json(prepared_meta.arguments)
                ),
                name=f"mcp-meta-operation:{tool_call_id}",
            )
            try:
                known, caller_cancelled = await _await_mcp_operation(operation_task)
            except McpPhysicalOutcomeUnknown as exc:
                raise KernelToolPhysicalInvocationError(
                    effect_class=(
                        "read_only"
                        if prepared_meta.executor.policy.effect_kind
                        is McpEffectKind.READ_ONLY
                        else "unknown_effect"
                    ),
                    error=exc,
                    timing="ON_TIME",
                    caller_cancelled=bool(getattr(exc, "caller_cancelled", False)),
                    physical_observation=_freeze_physical_observation(
                        invocation_started, observation_origin
                    ),
                ) from exc
            except BaseException as exc:
                raise KernelToolPhysicalInvocationError(
                    effect_class=(
                        "read_only"
                        if prepared_meta.executor.policy.effect_kind
                        is McpEffectKind.READ_ONLY
                        else "unknown_effect"
                    ),
                    error=exc,
                    timing="ON_TIME",
                    caller_cancelled=False,
                    physical_observation=_freeze_physical_observation(
                        invocation_started, observation_origin
                    ),
                ) from exc
            return _kernel_result_from_mcp_known(
                known,
                caller_cancelled=caller_cancelled,
                effect_kind=prepared_meta.executor.policy.effect_kind,
                physical_observation=_freeze_physical_observation(
                    invocation_started, observation_origin
                ),
            )
        if isinstance(binding.execution_policy, McpToolExecutionPolicyFact):
            generation = (
                invocation_context.surface_borrow.prepared.access.surface_generation
            )
            runtime = self._mcp_runtime_by_surface_generation.get(generation)
            if runtime is None:
                raise RuntimeError("MCP runtime generation was retired")
            executor = runtime.executors.get(tool_name)
            if executor is None:
                raise RuntimeError("MCP executor binding is unavailable")
            key = (generation, tool_call_id)
            permit = self._mcp_dispatch_permits.pop(key, None)
            if permit is None:
                raise RuntimeError("MCP dispatch admission permit is missing")
            if permit.state.value == "ADMITTED":
                permit.mark_attempt_accepted()
            operation_task = asyncio.create_task(
                executor.invoke(permit, arguments),
                name=f"mcp-tool-operation:{tool_call_id}",
            )
            try:
                known, caller_cancelled = await _await_mcp_operation(operation_task)
            except McpPhysicalOutcomeUnknown as exc:
                observation = _freeze_physical_observation(
                    invocation_started, observation_origin
                )
                raise KernelToolPhysicalInvocationError(
                    effect_class=(
                        "read_only"
                        if binding.execution_policy.effect_kind
                        is McpEffectKind.READ_ONLY
                        else "unknown_effect"
                    ),
                    error=exc,
                    timing="ON_TIME",
                    caller_cancelled=bool(getattr(exc, "caller_cancelled", False)),
                    physical_observation=observation,
                ) from exc
            except BaseException as exc:
                observation = _freeze_physical_observation(
                    invocation_started, observation_origin
                )
                raise KernelToolPhysicalInvocationError(
                    effect_class=(
                        "read_only"
                        if binding.execution_policy.effect_kind
                        is McpEffectKind.READ_ONLY
                        else "unknown_effect"
                    ),
                    error=exc,
                    timing="ON_TIME",
                    caller_cancelled=False,
                    physical_observation=observation,
                ) from exc
            text = known.content.decode("utf-8")
            return KernelToolResult(
                state=known.state,
                content=known.content,
                remote_identity=known.remote_identity,
                output_artifact_candidate=ToolOutputArtifactCandidate(
                    role="OUTPUT",
                    text=text,
                    source_coverage=ToolOutputSourceCoverage.COMPLETE,
                    original_utf8_bytes=len(known.content),
                    # MCP typed JSON is the complete public body, not the
                    # legacy terminal envelope whose JSON hint requires an
                    # ``output`` member.  Treat it as exact UTF-8 text so Round
                    # 1 archives these bytes without reinterpreting the shape.
                    source_format_hint=ToolOutputSourceFormatHint.TEXT,
                ),
                caller_cancelled_while_running=caller_cancelled,
                effect_class=(
                    "read_only"
                    if binding.execution_policy.effect_kind is McpEffectKind.READ_ONLY
                    else "unknown_effect"
                ),
                physical_observation=_freeze_physical_observation(
                    invocation_started, observation_origin
                ),
            )
        if tool_name in {
            "get_mcp_prompt",
            "read_mcp_resource",
        }:
            generation = (
                invocation_context.surface_borrow.prepared.access.surface_generation
            )
            runtime = self._mcp_runtime_by_surface_generation.get(generation)
            if runtime is None:
                raise RuntimeError("MCP runtime generation was retired")
            permit = self._mcp_dispatch_permits.pop((generation, tool_call_id), None)
            operation_task = asyncio.create_task(
                runtime.invoke_standard(
                    tool_name=tool_name,
                    arguments=arguments,
                    permit=permit,
                    scope_kind=(
                        invocation_context.surface_borrow.prepared.access.conversation_scope_kind
                    ),
                ),
                name=f"mcp-standard-operation:{tool_call_id}",
            )
            try:
                known, caller_cancelled = await _await_mcp_operation(operation_task)
            except BaseException as exc:
                if permit is not None and permit.state.value != "RELEASED":
                    with suppress(RuntimeError):
                        permit.release()
                raise KernelToolPhysicalInvocationError(
                    effect_class="read_only",
                    error=exc,
                    timing="ON_TIME",
                    caller_cancelled=False,
                    physical_observation=_freeze_physical_observation(
                        invocation_started, observation_origin
                    ),
                ) from exc
            text = known.content.decode("utf-8")
            return KernelToolResult(
                state=known.state,
                content=known.content,
                remote_identity=known.remote_identity,
                output_artifact_candidate=ToolOutputArtifactCandidate(
                    role="OUTPUT",
                    text=text,
                    source_coverage=ToolOutputSourceCoverage.COMPLETE,
                    original_utf8_bytes=len(known.content),
                    source_format_hint=ToolOutputSourceFormatHint.TEXT,
                ),
                caller_cancelled_while_running=caller_cancelled,
                effect_class="read_only",
                physical_observation=_freeze_physical_observation(
                    invocation_started, observation_origin
                ),
            )
        if self._subagent is not None and tool_name in self._subagent.tool_names:
            result = await self._subagent.invoke(
                tool_name=tool_name,
                arguments=arguments,
                invocation_context=invocation_context,
            )
            return replace(
                result,
                physical_observation=_freeze_physical_observation(
                    invocation_started, observation_origin
                ),
            )
        if self._memory is not None and tool_name in self._memory.tool_names:
            result = await self._memory.invoke(
                tool_name=tool_name,
                arguments=arguments,
                invocation_context=invocation_context,
            )
            return replace(
                result,
                physical_observation=_freeze_physical_observation(
                    invocation_started, observation_origin
                ),
            )
        tool = self._tools[tool_name]
        if isinstance(tool, _DirectPlanControlTool):
            raise RuntimeError("Plan control escaped the runner batch barrier")
        if isinstance(tool, TodoTool):
            candidate = parse_todo_replacement(arguments)
            counts = {
                "pending": candidate.pending_count,
                "in_progress": candidate.in_progress_count,
                "completed": candidate.completed_count,
                "total": len(candidate.ordered_items),
            }
            acknowledgement = json.dumps(
                {
                    "status": ("CLEARED" if not candidate.ordered_items else "UPDATED"),
                    "counts": counts,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            try:
                prepared = self._todo_owner.prepare_replace(
                    scope_kind=(
                        invocation_context.surface_borrow.prepared.access.conversation_scope_kind
                    ),
                    scope_subagent_task_id=(invocation_context.scope_subagent_task_id),
                    exact_turn_id=turn_id,
                    attempt_id=attempt_id,
                    proposed_result_entry_id=invocation_context.result_entry_id,
                    candidate=candidate,
                    acknowledgement=acknowledgement,
                )
            except LookupError:
                return KernelToolResult(
                    # The attempt already exists; scope expiry is a known
                    # rejection, not a no-dispatch TOOL_UNAVAILABLE result.
                    state="APPLICATION_ERROR",
                    content=b'{"error":"todo scope is no longer active"}',
                    effect_class="read_only",
                    physical_observation=_freeze_physical_observation(
                        invocation_started, observation_origin
                    ),
                )
            token = ProcessLocalEffectSettlementToken(
                prepared.token_id,
                prepared,
            )
            self._todo_settlements[token.token_id] = token
            return KernelToolResult(
                state="SUCCESS",
                content=acknowledgement,
                process_local_settlement=token,
                effect_class="read_only",
                physical_observation=_freeze_physical_observation(
                    invocation_started, observation_origin
                ),
            )
        call = ToolCall(
            id=tool_call_id,
            name=tool_name,
            arguments=dict(arguments),
        )
        if isinstance(tool, VisualizationRenderTool):
            source, review = parse_visualization_source(dict(arguments))
            try:
                source = tool.freeze_source(source)
            except (ValueError, OSError) as exc:
                return KernelToolResult(
                    state="APPLICATION_ERROR",
                    content=json.dumps(
                        {"error": "VISUALIZATION_PATH_INVALID", "detail": str(exc)},
                        ensure_ascii=False,
                    ).encode("utf-8"),
                    effect_class="read_only",
                )
            preview: FrozenPromptContent | None = None
            preview_failure: str | None = None
            if review:
                if invocation_context.input_modalities is not None and (
                    "image" not in invocation_context.input_modalities
                ):
                    preview_failure = "MODEL_IMAGE_INPUT_UNSUPPORTED"
                else:
                    allowance = invocation_context.image_resource_allowance
                    if allowance is None or (
                        allowance.tool_call_id != tool_call_id
                        or allowance.executor_binding_fingerprint
                        != binding.executor_binding_fingerprint
                    ):
                        raise RuntimeError(
                            "visualization review resource allowance does not exact-join"
                        )
                    deadline = self._deadlines.deadline(
                        KernelWatchdogOwner.NONTERMINAL_TOOL_INVOCATION
                    )
                    try:
                        if source.kind is VisualizationSourceKind.PATH:
                            html = await asyncio.to_thread(
                                read_visualization_file, Path(source.value)
                            )
                            if html is None:
                                raise VisualizationScreenshotError("HTML_NOT_FOUND")
                        else:
                            if self._visualization_reference_read_port is None:
                                raise RuntimeError(
                                    "visualization reference read owner is unavailable"
                                )
                            html = await asyncio.to_thread(
                                self._visualization_reference_read_port.read_ref,
                                source.value,
                                deadline_monotonic=deadline,
                            )
                        png = await self._visualization_screenshots.render(
                            html, deadline_monotonic=deadline
                        )
                        if self._image_validator is None:
                            raise RuntimeError("image validation owner is unavailable")
                        image = await self._image_validator.freeze_local_image(
                            png, deadline_monotonic=deadline
                        )
                        candidate_content = FrozenPromptContent(
                            (
                                LLMTextPart(
                                    "Visualization is subscribed for this response. "
                                    "Current preview attached."
                                ),
                                image,
                            )
                        )
                        increment = allowance.quote_owner.quote(
                            tool_call_id=tool_call_id,
                            source=source,
                            content=candidate_content,
                        )
                        if (
                            increment.canonical_bytes > allowance.canonical_bytes
                            or increment.logical_bytes > allowance.logical_bytes
                            or increment.wire_bytes > allowance.wire_bytes
                            or increment.input_tokens > allowance.input_tokens
                        ):
                            raise VisualizationScreenshotError("IMAGE_RESOURCE_EXCEEDED")
                        preview = candidate_content
                    except asyncio.CancelledError:
                        raise
                    except (
                        VisualizationScreenshotError,
                        PromptImageValidationError,
                        TimeoutError,
                        KeyError,
                        ValueError,
                        OSError,
                    ) as exc:
                        preview_failure = type(exc).__name__
                        if isinstance(exc, VisualizationScreenshotError):
                            preview_failure = str(exc).split(":", 1)[0]
            prepared = VisualizationSubscription(
                turn_id=turn_id,
                source_result_entry_id=invocation_context.result_entry_id,
                source=source,
            )
            token = ProcessLocalEffectSettlementToken(
                f"visualization:{invocation_context.result_entry_id}", prepared
            )
            with self._surface_lock:
                self._visualization_settlements[token.token_id] = token
            message = "Visualization is subscribed for this response."
            if review and preview is None:
                message += (
                    " Current preview was not generated: "
                    + (preview_failure or "preview unavailable")
                    + "."
                )
            return KernelToolResult(
                state="SUCCESS", content=(preview or message.encode("utf-8")),
                process_local_settlement=token, effect_class="read_only",
                physical_observation=_freeze_physical_observation(
                    invocation_started, observation_origin
                ),
            )
        if isinstance(tool, ViewImageTool):
            source = parse_view_image_source(arguments)
            if (
                invocation_context.input_modalities is not None
                and "image" not in invocation_context.input_modalities
            ):
                return KernelToolResult(
                    state="APPLICATION_ERROR",
                    content=b'{"error":"MODEL_IMAGE_INPUT_UNSUPPORTED"}',
                    effect_class="read_only",
                    physical_observation=_freeze_physical_observation(
                        invocation_started, observation_origin
                    ),
                )
            allowance = invocation_context.image_resource_allowance
            if allowance is None:
                raise RuntimeError("view_image invocation lacks its resource allowance")
            if (
                allowance.tool_call_id != tool_call_id
                or allowance.executor_binding_fingerprint
                != binding.executor_binding_fingerprint
            ):
                raise RuntimeError("view_image resource allowance does not exact-join")
            if (
                source.kind is ViewImageSourceKind.PATH
                and self._image_validator is None
            ):
                raise RuntimeError("view_image validation owner is unavailable")
            if (
                source.kind is ViewImageSourceKind.IMAGE_REF
                and self._image_reference_read_port is None
            ):
                raise RuntimeError("view_image reference read owner is unavailable")
            maximum_bytes = min(
                allowance.canonical_bytes,
                allowance.logical_bytes,
                3 * (allowance.wire_bytes // 4),
            )
            deadline = self._deadlines.deadline(
                KernelWatchdogOwner.NONTERMINAL_TOOL_INVOCATION
            )
            if source.kind is ViewImageSourceKind.PATH:
                physical = await self._physical_io.run_tool_invocation(
                    _read_local_image_candidate,
                    tool,
                    call,
                    maximum_bytes,
                    deadline_monotonic=deadline,
                )
            else:
                assert self._image_reference_read_port is not None
                physical = await self._physical_io.run_tool_invocation(
                    _read_canonical_image_reference,
                    self._image_reference_read_port,
                    invocation_context.session_id,
                    invocation_context.workspace_id,
                    source.value,
                    maximum_bytes,
                    deadline_monotonic=deadline,
                )
            if physical.disposition is PhysicalToolInvocationDisposition.RAISED:
                assert physical.error is not None
                if isinstance(
                    physical.error,
                    (CanonicalImageReferenceUnavailable,),
                ):
                    return KernelToolResult(
                        state="APPLICATION_ERROR",
                        content=b'{"error":"IMAGE_REFERENCE_UNAVAILABLE"}',
                        effect_class="read_only",
                        physical_timing=physical.timing.value,
                        caller_cancelled_while_running=physical.caller_cancelled,
                        physical_observation=(
                            None
                            if physical.observation is None
                            else replace(
                                physical.observation,
                                observation_origin_kind=observation_origin,
                            )
                        ),
                    )
                if isinstance(
                    physical.error,
                    CanonicalImageReferenceResourceExceeded,
                ):
                    return KernelToolResult(
                        state="APPLICATION_ERROR",
                        content=b'{"error":"IMAGE_RESOURCE_EXCEEDED"}',
                        effect_class="read_only",
                        physical_timing=physical.timing.value,
                        caller_cancelled_while_running=physical.caller_cancelled,
                        physical_observation=(
                            None
                            if physical.observation is None
                            else replace(
                                physical.observation,
                                observation_origin_kind=observation_origin,
                            )
                        ),
                    )
                raise KernelToolPhysicalInvocationError(
                    effect_class="read_only",
                    error=physical.error,
                    timing=physical.timing.value,
                    caller_cancelled=physical.caller_cancelled,
                    physical_observation=(
                        None
                        if physical.observation is None
                        else replace(
                            physical.observation,
                            observation_origin_kind=observation_origin,
                        )
                    ),
                )
            if physical.caller_cancelled:
                raise asyncio.CancelledError
            candidate = physical.value
            if isinstance(candidate, ToolExecutionResult):
                if not isinstance(candidate.output, str):
                    raise TypeError("view_image read failure must be text")
                return KernelToolResult(
                    state="APPLICATION_ERROR",
                    content=candidate.output.encode("utf-8"),
                    effect_class="read_only",
                    physical_timing=physical.timing.value,
                    caller_cancelled_while_running=physical.caller_cancelled,
                    physical_observation=(
                        None
                        if physical.observation is None
                        else replace(
                            physical.observation,
                            observation_origin_kind=observation_origin,
                        )
                    ),
                )
            if source.kind is ViewImageSourceKind.PATH:
                if not isinstance(candidate, LocalImageReadCandidate):
                    raise TypeError("view_image path read returned an invalid candidate")
                assert self._image_validator is not None
                try:
                    image = await self._image_validator.freeze_local_image(
                        candidate.payload,
                        deadline_monotonic=deadline,
                    )
                except TimeoutError:
                    return KernelToolResult(
                        state="SYSTEM_ERROR",
                        content=b'{"error":"IMAGE_VALIDATION_DEADLINE_EXPIRED"}',
                        effect_class="read_only",
                        physical_timing=(
                            PhysicalToolInvocationTiming.LATE_AFTER_WATCHDOG.value
                        ),
                        physical_observation=_freeze_physical_observation(
                            invocation_started, observation_origin
                        ),
                    )
                except PromptImageValidationError as exc:
                    code = (
                        "IMAGE_FORMAT_UNSUPPORTED"
                        if "unsupported" in str(exc).lower()
                        else "IMAGE_DECODE_FAILED"
                    )
                    return KernelToolResult(
                        state="APPLICATION_ERROR",
                        content=json.dumps(
                            {"error": code, "path": candidate.requested_path},
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ).encode("utf-8"),
                        effect_class="read_only",
                        physical_timing=(
                            PhysicalToolInvocationTiming.LATE_AFTER_WATCHDOG.value
                            if monotonic() >= deadline
                            else physical.timing.value
                        ),
                        physical_observation=_freeze_physical_observation(
                            invocation_started, observation_origin
                        ),
                    )
            else:
                if not isinstance(candidate, LLMImagePart):
                    raise TypeError(
                        "view_image reference read returned an invalid candidate"
                    )
                image = candidate
            content = FrozenPromptContent((LLMTextPart("Image loaded."), image))
            increment = allowance.quote_owner.quote(
                tool_call_id=tool_call_id,
                source=source,
                content=content,
            )
            if (
                increment.canonical_bytes > allowance.canonical_bytes
                or increment.logical_bytes > allowance.logical_bytes
                or increment.wire_bytes > allowance.wire_bytes
                or increment.input_tokens > allowance.input_tokens
            ):
                return KernelToolResult(
                    state="APPLICATION_ERROR",
                    content=b'{"error":"IMAGE_RESOURCE_EXCEEDED"}',
                    effect_class="read_only",
                    physical_timing=(
                        PhysicalToolInvocationTiming.LATE_AFTER_WATCHDOG.value
                        if monotonic() >= deadline
                        else physical.timing.value
                    ),
                    physical_observation=_freeze_physical_observation(
                        invocation_started, observation_origin
                    ),
                )
            return KernelToolResult(
                state="SUCCESS",
                content=content,
                effect_class="read_only",
                physical_timing=(
                    PhysicalToolInvocationTiming.LATE_AFTER_WATCHDOG.value
                    if monotonic() >= deadline
                    else physical.timing.value
                ),
                physical_observation=_freeze_physical_observation(
                    invocation_started, observation_origin
                ),
            )
        settlement_token: ProcessLocalEffectSettlementToken | None = None
        if isinstance(tool, _DirectTerminalMonitorTool):
            result, settlement_token = self._invoke_terminal_monitor(
                tool=tool,
                call=call,
                context=invocation_context,
            )
        elif isinstance(tool, (_DirectTerminalTool, _DirectTerminalProcessTool)):
            origin = TerminalProcessOrigin(
                turn_id=turn_id,
                conversation_scope_kind=(invocation_context.conversation_scope_kind),
                scope_subagent_task_id=invocation_context.scope_subagent_task_id,
            )
            owner = (
                KernelWatchdogOwner.TERMINAL_FOREGROUND_DECISION
                if isinstance(tool, _DirectTerminalTool)
                else KernelWatchdogOwner.NONTERMINAL_TOOL_INVOCATION
            )
            physical = await self._physical_io.run_tool_invocation(
                _execute_terminal_tool_call,
                tool,
                call,
                live_sink,
                origin,
                attempt_id,
                invocation_context.effective_permission_mode,
                deadline_monotonic=self._deadlines.deadline(owner),
                on_caller_cancelled=(
                    lambda: tool.manager.process_registry.abort_foreground_decision(
                        attempt_id
                    )
                    if isinstance(tool, _DirectTerminalTool)
                    else None
                ),
            )
        else:
            physical = await self._physical_io.run_tool_invocation(
                _execute_tool_call,
                tool,
                call,
                invocation_context.effective_permission_mode,
                invocation_context.permission_confirmation_granted,
                deadline_monotonic=self._deadlines.deadline(
                    KernelWatchdogOwner.NONTERMINAL_TOOL_INVOCATION
                ),
            )
        if not isinstance(tool, _DirectTerminalMonitorTool):
            if physical.disposition is PhysicalToolInvocationDisposition.RAISED:
                assert physical.error is not None
                effect_class = _physical_effect_class(tool_name, arguments)
                observation = (
                    None
                    if physical.observation is None
                    else replace(
                        physical.observation,
                        observation_origin_kind=observation_origin,
                    )
                )
                raise KernelToolPhysicalInvocationError(
                    effect_class=effect_class,
                    error=physical.error,
                    timing=physical.timing.value,
                    caller_cancelled=physical.caller_cancelled,
                    physical_observation=observation,
                )
            result = physical.value
            if not isinstance(result, ToolExecutionResult):
                raise TypeError("physical tool returned an invalid result carrier")
        encoded = (
            result.output.encode("utf-8")
            if isinstance(result.output, str)
            else result.output
        )
        state = {
            ToolResultState.SUCCESS: "SUCCESS",
            ToolResultState.ERROR: "APPLICATION_ERROR",
            ToolResultState.INTERRUPTED: "CANCELLED",
            ToolResultState.DENIED: "APPLICATION_ERROR",
            ToolResultState.RUNNING: "SUCCESS",
        }[result.status]
        remote_identity: str | None = None
        if tool_name in {"terminal", "terminal_process"}:
            if not isinstance(result.output, str):
                raise TypeError("terminal Tool output must be text")
            try:
                payload = json.loads(result.output)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                process_id = payload.get("process_id")
                if isinstance(process_id, str) and process_id:
                    remote_identity = process_id
        return KernelToolResult(
            state=state,
            content=encoded,
            remote_identity=remote_identity,
            output_artifact_candidate=result.output_artifact_candidate,
            artifact_source_read=result.artifact_source_read,
            process_local_settlement=settlement_token,
            physical_timing=(
                "ON_TIME"
                if isinstance(tool, _DirectTerminalMonitorTool)
                else physical.timing.value
            ),
            caller_cancelled_while_running=(
                False
                if isinstance(tool, _DirectTerminalMonitorTool)
                else physical.caller_cancelled
            ),
            physical_observation=(
                _freeze_physical_observation(invocation_started, observation_origin)
                if isinstance(tool, _DirectTerminalMonitorTool)
                else None
                if physical.observation is None
                else replace(
                    physical.observation,
                    observation_origin_kind=observation_origin,
                )
            ),
            trusted_observation=_validated_terminal_observation_supplement(
                tool=tool,
                tool_name=tool_name,
                arguments=arguments,
                claimed=result.trusted_observation,
            ),
            model_visible_memory_fact_ids=result.model_visible_memory_fact_ids,
        )

    def _invoke_terminal_monitor(
        self,
        *,
        tool: _DirectTerminalMonitorTool,
        call: ToolCall,
        context: KernelToolInvocationContext,
    ) -> tuple[ToolExecutionResult, ProcessLocalEffectSettlementToken | None]:
        request = parse_terminal_monitor_input(call.arguments)
        if context.conversation_scope_kind != "ROOT":
            return _terminal_monitor_rejected(call, "ROOT_SCOPE_REQUIRED"), None
        if request.action == "list":
            return (
                _success(
                    call,
                    {
                        "status": "INVENTORY",
                        "monitors": list(tool.coordinator.list_current()),
                    },
                ),
                None,
            )
        if request.action == "cancel":
            outcome = tool.coordinator.cancel(request.monitor_id)
            return (
                _success(
                    call,
                    {
                        "status": "CANCELLED" if outcome == "cancelled" else "REJECTED",
                        "monitor_id": request.monitor_id,
                        "cancellation_outcome": outcome,
                    },
                ),
                None,
            )
        if not isinstance(request, TerminalMonitorRegisterInput):
            raise AssertionError("terminal monitor action union is invalid")
        output_condition = request.conditions.output
        try:
            prepared = tool.coordinator.prepare_registration(
                process_id=request.process_id,
                origin_turn_id=context.turn_id,
                origin_attempt_id=context.attempt_id,
                origin_result_entry_id=context.result_entry_id,
                writer_generation=context.host_owner_epoch,
                authorization_reference=context.authorization_reference,
                policy=TerminalMonitorPolicy(
                    min_new_output_chars=(
                        None
                        if output_condition is None
                        else output_condition.min_new_output_chars
                    ),
                    quiet_period_ms=(
                        500
                        if output_condition is None
                        else output_condition.quiet_period_ms
                    ),
                    heartbeat_interval_seconds=(
                        request.conditions.heartbeat_interval_seconds
                    ),
                    max_output_chars=request.delivery.max_output_chars,
                    minimum_progress_interval_seconds=(
                        request.delivery.minimum_progress_observation_interval_seconds
                    ),
                    maximum_duration_seconds=request.lifetime.maximum_duration_seconds,
                ),
            )
        except TerminalMonitorRejected as exc:
            return _terminal_monitor_rejected(call, exc.reason.value), None
        token = ProcessLocalEffectSettlementToken(
            prepared.token_id,
            prepared,
        )
        self._process_local_settlements[token.token_id] = token
        return (
            _success(
                call,
                {
                    "status": "REGISTERED",
                    "monitor_id": prepared.monitor_id,
                    "process_id": prepared.process_id,
                    "baseline_cursor": prepared.baseline_cursor,
                    "expires_at": prepared.expires_at.isoformat(),
                    "policy": {
                        "completion": True,
                        "output": output_condition is not None,
                        "heartbeat_interval_seconds": (
                            request.conditions.heartbeat_interval_seconds
                        ),
                    },
                },
            ),
            token,
        )

    def _terminal_process_completed(
        self,
        info: TerminalProcessInfo,
        snapshot: TerminalOutputSnapshot,
    ) -> None:
        process_id = info.process_id
        generation_id = f"terminal-process:{process_id}"
        origin = info.origin
        self._live_bus.offer_nowait(
            event_type=LiveEventType.TERMINAL_PROCESS_COMPLETED,
            session_id=self._session_id,
            turn_id=origin.turn_id,
            draft_identity=generation_id,
            payload=TerminalProcessCompletedPayload(
                process_id,
                info.status,
                info.exit_code,
                len(snapshot.text.encode("utf-8")),
                live_digest(snapshot.text),
            ),
            scope_kind=origin.conversation_scope_kind,
            scope_subagent_task_id=origin.scope_subagent_task_id,
            channel_kind=LiveChannelKind.TERMINAL_EXTENSION,
            generation_id=generation_id,
            block_id=process_id,
            block_ordinal=0,
            block_kind=LiveBlockKind.OPERATIONAL,
        )
        self._terminal_monitor.process_completed(
            process_id, status=info.status, exit_code=info.exit_code
        )

    async def settle_process_local_effect(
        self,
        token: ProcessLocalEffectSettlementToken,
        disposition: ProcessLocalEffectSettlementDisposition,
    ) -> ProcessLocalEffectSettlementResult:
        if isinstance(token.prepared, VisualizationSubscription):
            with self._surface_lock:
                retained = self._visualization_settlements.get(token.token_id)
                if retained is not token:
                    if disposition is ProcessLocalEffectSettlementDisposition.COMMITTED:
                        raise RuntimeError("committed visualization settlement is absent")
                    return ProcessLocalEffectSettlementResult(
                        ProcessLocalEffectSettlementOutcome.DISCARDED
                    )
                self._visualization_settlements.pop(token.token_id, None)
                if disposition is ProcessLocalEffectSettlementDisposition.COMMITTED:
                    prepared = token.prepared
                    bucket = self._visualization_subscriptions.setdefault(
                        prepared.turn_id, {}
                    )
                    bucket.setdefault(
                        (prepared.source.kind.value, prepared.source.value), prepared
                    )
                    return ProcessLocalEffectSettlementResult(
                        ProcessLocalEffectSettlementOutcome.INSTALLED
                    )
                return ProcessLocalEffectSettlementResult(
                    ProcessLocalEffectSettlementOutcome.DISCARDED
                )
        if isinstance(token.prepared, PreparedNewMcpToolRefSettlement):
            retained = self._mcp_ref_settlements.get(token.token_id)
            if retained is not token:
                if disposition is ProcessLocalEffectSettlementDisposition.COMMITTED:
                    raise RuntimeError("committed MCP ref settlement token is absent")
                return ProcessLocalEffectSettlementResult(
                    ProcessLocalEffectSettlementOutcome.DISCARDED
                )
            self._mcp_ref_settlements.pop(token.token_id, None)
            self._mcp_meta_refs.settle(
                prepared=token.prepared,
                committed=(
                    disposition is ProcessLocalEffectSettlementDisposition.COMMITTED
                ),
            )
            return ProcessLocalEffectSettlementResult(
                ProcessLocalEffectSettlementOutcome.INSTALLED
                if disposition is ProcessLocalEffectSettlementDisposition.COMMITTED
                else ProcessLocalEffectSettlementOutcome.DISCARDED
            )
        if isinstance(token.prepared, PreparedTodoReplacement):
            retained = self._todo_settlements.get(token.token_id)
            if retained is not token:
                if disposition is ProcessLocalEffectSettlementDisposition.COMMITTED:
                    raise RuntimeError("committed TODO settlement token is absent")
                return ProcessLocalEffectSettlementResult(
                    ProcessLocalEffectSettlementOutcome.DISCARDED
                )
            if disposition is ProcessLocalEffectSettlementDisposition.COMMITTED:
                installation = self._todo_owner.commit(token.prepared)
                outcome = ProcessLocalEffectSettlementOutcome.INSTALLED
            else:
                self._todo_owner.discard(token.prepared)
                installation = None
                outcome = ProcessLocalEffectSettlementOutcome.DISCARDED
            self._todo_settlements.pop(token.token_id, None)
            if installation is not None:
                self._offer_todo_installation(installation)
            return ProcessLocalEffectSettlementResult(outcome)
        if not isinstance(token.prepared, PreparedTerminalMonitorRegistration):
            raise TypeError("process-local settlement token kind is unknown")
        retained = self._process_local_settlements.get(token.token_id)
        if retained is None:
            if disposition is ProcessLocalEffectSettlementDisposition.COMMITTED:
                raise RuntimeError("committed process-local settlement token is absent")
            return ProcessLocalEffectSettlementResult(
                ProcessLocalEffectSettlementOutcome.DISCARDED
            )
        if retained is not token:
            raise RuntimeError("process-local settlement token conflicts")
        self._process_local_settlements.pop(token.token_id, None)
        self._terminal_monitor.settle_registration(
            token.prepared,
            committed=(
                disposition is ProcessLocalEffectSettlementDisposition.COMMITTED
            ),
        )
        return ProcessLocalEffectSettlementResult(
            ProcessLocalEffectSettlementOutcome.INSTALLED
            if disposition is ProcessLocalEffectSettlementDisposition.COMMITTED
            else ProcessLocalEffectSettlementOutcome.DISCARDED
        )

    def offer_todo_close(self, projection: FrozenTodoCloseProjection | None) -> None:
        """Best-effort projection of an exact owner-controlled run closure."""

        if projection is None:
            return
        identity = projection.run_identity
        self._offer_todo_snapshot(
            todo_run_id=identity.todo_run_id,
            todo_revision=projection.closing_revision,
            disposition="CLOSED",
            ordered_items=(),
            pending_count=0,
            in_progress_count=0,
            completed_count=0,
            turn_id=projection.last_turn_id,
            scope_kind=identity.scope_kind,
            scope_subagent_task_id=identity.subagent_task_id,
        )

    def _offer_todo_installation(self, installation: TodoInstallation) -> None:
        snapshot = installation.installed_snapshot
        identity = snapshot.run_identity
        self._offer_todo_snapshot(
            todo_run_id=identity.todo_run_id,
            todo_revision=snapshot.revision,
            disposition=installation.disposition.value,
            ordered_items=tuple(
                TodoLiveItemProjection(item.ordinal, item.text, item.status.value)
                for item in snapshot.ordered_items
            ),
            pending_count=snapshot.pending_count,
            in_progress_count=snapshot.in_progress_count,
            completed_count=snapshot.completed_count,
            turn_id=installation.turn_id,
            scope_kind=identity.scope_kind,
            scope_subagent_task_id=identity.subagent_task_id,
        )

    def _offer_todo_snapshot(
        self,
        *,
        todo_run_id: str,
        todo_revision: int,
        disposition: str,
        ordered_items: tuple[TodoLiveItemProjection, ...],
        pending_count: int,
        in_progress_count: int,
        completed_count: int,
        turn_id: str,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
    ) -> None:
        draft_identity = f"todo:{todo_run_id}:{todo_revision}"
        try:
            self._live_bus.offer_nowait(
                event_type=LiveEventType.TODO_SNAPSHOT_UPDATED,
                session_id=self._session_id,
                turn_id=turn_id,
                draft_identity=draft_identity,
                payload=TodoSnapshotUpdatedPayload(
                    todo_run_id=todo_run_id,
                    todo_revision=todo_revision,
                    disposition=disposition,
                    ordered_items=ordered_items,
                    pending_count=pending_count,
                    in_progress_count=in_progress_count,
                    completed_count=completed_count,
                ),
                scope_kind=scope_kind.value,
                scope_subagent_task_id=scope_subagent_task_id,
                channel_kind=LiveChannelKind.TERMINAL_EXTENSION,
                generation_id=f"todo:{todo_run_id}",
                proposed_entry_id=None,
                block_id=draft_identity,
                block_ordinal=0,
                block_kind=LiveBlockKind.OPERATIONAL,
            )
        except BaseException:
            # Live delivery is a disposable observation plane.  It cannot
            # roll back the already-installed process-local snapshot.
            pass

    async def aclose(self, *, timeout_seconds: float = 5.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("tool close timeout must be positive")
        async with self._close_async_lock:
            if self._physically_closed:
                return
            deadline = monotonic() + timeout_seconds
            with self._surface_condition:
                self._closed = True
                self._builtin_composition_state = BuiltinCompositionState.CLOSED
                self._surface_condition.notify_all()
            # A surface borrow can be held by an in-flight Terminal call whose
            # bounded physical wait only exits after its process group is
            # terminated.  Seal the surface first, stop those process-local
            # owners, then drain the immutable borrow before closing the
            # general thread owner.  Non-Terminal tools remain bounded by the
            # same borrow deadline and are never replaced or detached.
            close_error: BaseException | None = None
            try:
                await self._visualization_screenshots.aclose()
                await self._stop_terminal_physical_owners_locked(deadline)
            except BaseException as exc:
                close_error = exc
            try:
                if await asyncio.to_thread(self._wait_for_surface_borrows, deadline):
                    close_error = close_error or TimeoutError(
                        "tool surface borrows exited after close deadline"
                    )
            except BaseException as exc:
                close_error = close_error or exc
            with self._surface_lock:
                permits = tuple(self._mcp_dispatch_permits.values())
                self._mcp_dispatch_permits.clear()
                self._mcp_confirmation_admissions.clear()
                self._mcp_meta_invocations.clear()
                self._installed_epoch_by_borrow.clear()
                runtimes = tuple(
                    dict.fromkeys(self._mcp_runtime_by_surface_generation.values())
                )
                self._mcp_runtime_by_surface_generation.clear()
                self._mcp_current = None
            self._mcp_meta_refs.close()
            self._mcp_ref_settlements.clear()
            self._todo_settlements.clear()
            self._visualization_settlements.clear()
            self._visualization_subscriptions.clear()
            for permit in permits:
                try:
                    permit.release()
                except BaseException as exc:
                    close_error = close_error or exc
            for runtime in runtimes:
                try:
                    runtime.release()
                except BaseException as exc:
                    close_error = close_error or exc
            try:
                await self._physical_io.aclose(deadline_monotonic=deadline)
            except BaseException as exc:
                close_error = close_error or exc
            self._physically_closed = True
            if close_error is not None:
                raise close_error

    async def stop_terminal_physical_owners(
        self, *, timeout_seconds: float = 5.0
    ) -> None:
        """Stop monitor/process owners before awaiting cancelled tool threads.

        ``asyncio`` cannot cancel a thread blocked in a foreground Terminal
        wait.  Host close calls this seam first so process-group termination
        makes that exact physical invocation return; the normal ``aclose``
        then drains KernelSessionIO without starting a replacement owner.
        """

        if timeout_seconds <= 0:
            raise ValueError("tool close timeout must be positive")
        async with self._close_async_lock:
            await self._stop_terminal_physical_owners_locked(
                monotonic() + timeout_seconds
            )

    async def _stop_terminal_physical_owners_locked(
        self, deadline_monotonic: float
    ) -> None:
        if self._terminal_physically_closed:
            return
        with self._close_lock:
            self._closed = True
        with self._surface_condition:
            self._closed = True
            self._surface_condition.notify_all()
        for token_id, token in tuple(self._process_local_settlements.items()):
            prepared = token.prepared
            if not isinstance(prepared, PreparedTerminalMonitorRegistration):
                raise RuntimeError("terminal settlement carrier kind drifted")
            self._terminal_monitor.settle_registration(
                prepared,
                committed=False,
            )
            self._process_local_settlements.pop(token_id, None)
        if self._terminal_monitor_close_task is None:
            self._terminal_monitor_close_task = asyncio.create_task(
                self._close_terminal_monitor_worker(deadline_monotonic)
            )
        monitor_late = await _join_close_task(
            self._terminal_monitor_close_task,
            deadline_monotonic=deadline_monotonic,
        )
        self._terminal_monitor_close_task.result()
        if self._terminal_release_task is None:
            self._terminal_release_task = asyncio.create_task(
                self._release_terminal_owner_worker(deadline_monotonic)
            )
        release_late = await _join_close_task(
            self._terminal_release_task,
            deadline_monotonic=deadline_monotonic,
        )
        self._terminal_release_task.result()
        self._terminal_physically_closed = True
        if monitor_late or release_late:
            raise TimeoutError("Terminal owner exited after close deadline")

    async def _close_terminal_monitor_worker(self, deadline_monotonic: float) -> None:
        try:
            await asyncio.to_thread(
                self._terminal_monitor.stop_admission_and_close,
                timeout_seconds=max(0.001, deadline_monotonic - monotonic()),
            )
        except TimeoutError:
            await asyncio.to_thread(self._terminal_monitor.join_physical_after_close)
            raise

    async def _release_terminal_owner_worker(self, deadline_monotonic: float) -> None:
        try:
            await asyncio.to_thread(
                self._terminal.release_owner,
                self._host_owner_id,
                timeout_seconds=max(0.001, deadline_monotonic - monotonic()),
            )
        except TimeoutError:
            await asyncio.to_thread(
                self._terminal.release_owner_and_join,
                self._host_owner_id,
            )
            raise

    def _wait_for_surface_borrows(self, deadline_monotonic: float) -> bool:
        deadline_expired = False
        with self._surface_condition:
            while self._surface_borrows:
                remaining = deadline_monotonic - monotonic()
                if remaining <= 0:
                    deadline_expired = True
                    self._surface_condition.wait()
                else:
                    self._surface_condition.wait(timeout=remaining)
        return deadline_expired


async def _join_close_task(
    task: asyncio.Task[object], *, deadline_monotonic: float
) -> bool:
    """Join an admitted close worker and report logical watchdog expiry."""

    deadline_expired = False
    if not task.done():
        remaining = deadline_monotonic - monotonic()
        if remaining > 0:
            done, _pending = await asyncio.wait((task,), timeout=remaining)
            deadline_expired = not done
        else:
            deadline_expired = True
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    return deadline_expired


async def _await_mcp_operation(
    task: asyncio.Task[McpKnownToolResult],
) -> tuple[McpKnownToolResult, bool]:
    """Keep one admitted MCP physical call attached through waiter cancellation."""

    caller_cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            caller_cancelled = True
            continue
        except BaseException:
            break
    try:
        return task.result(), caller_cancelled
    except McpPhysicalOutcomeUnknown as exc:
        exc.caller_cancelled = caller_cancelled  # type: ignore[attr-defined]
        raise


def _execute_tool_call(
    tool: Tool,
    call: ToolCall,
    effective_permission_mode: PermissionMode,
    permission_confirmation_granted: bool,
    *,
    deadline_monotonic: float,
) -> ToolExecutionResult:
    # The closed Tool API predates absolute deadlines.  KernelSessionIO owns
    # the physical thread and makes a close timeout explicit; adapters with
    # their own timeouts continue to enforce them inside execute().
    del deadline_monotonic
    if isinstance(tool, (EditFileTool, WriteFileTool)):
        write_scope = (
            WritePathScope.HOST_LOCAL
            if effective_permission_mode is PermissionMode.BYPASS_PERMISSIONS
            or permission_confirmation_granted
            else WritePathScope.WORKSPACE
        )
        return tool.execute(call, write_scope=write_scope)
    return tool.execute(call)


def _read_local_image_candidate(
    tool: ViewImageTool,
    call: ToolCall,
    maximum_bytes: int,
    *,
    deadline_monotonic: float,
) -> LocalImageReadCandidate | ToolExecutionResult:
    del deadline_monotonic
    return tool.read_bounded(call, maximum_bytes=maximum_bytes)


def _read_canonical_image_reference(
    port: CanonicalImageReferenceReadPort,
    session_id: str,
    workspace_id: str,
    image_ref: str,
    maximum_bytes: int,
    *,
    deadline_monotonic: float,
) -> LLMImagePart:
    return port.read_image(
        session_id=session_id,
        workspace_id=workspace_id,
        image_ref=image_ref,
        maximum_encoded_bytes=maximum_bytes,
        deadline_monotonic=deadline_monotonic,
    )


def _physical_effect_class(tool_name: str, arguments: Mapping[str, object]) -> str:
    if tool_name == "terminal_process":
        action = arguments.get("action")
        for candidate, effect_class in _TERMINAL_PROCESS_ACTION_EFFECTS:
            if action == candidate:
                return effect_class
        raise RuntimeError("terminal_process action escaped its closed catalog")
    if tool_name == "terminal":
        return "TERMINAL_EFFECT"
    return builtin_tool_catalog_entry(tool_name).recovery_contract.severity


def _execute_terminal_tool_call(
    tool: _DirectTerminalTool | _DirectTerminalProcessTool,
    call: ToolCall,
    live_sink: KernelToolLiveSink | None,
    origin: TerminalProcessOrigin,
    decision_attempt_id: str,
    effective_permission_mode: PermissionMode,
    *,
    deadline_monotonic: float,
) -> ToolExecutionResult:
    if isinstance(tool, _DirectTerminalTool):
        return tool.execute(
            call,
            live_sink=live_sink,
            origin=origin,
            decision_attempt_id=decision_attempt_id,
            decision_deadline_monotonic=deadline_monotonic,
            effective_permission_mode=effective_permission_mode,
        )
    try:
        return tool.execute(call, live_sink=live_sink)
    except InvalidTerminalOutputCursor:
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            status=ToolResultState.ERROR,
            output=json.dumps(
                {
                    "error": "INVALID_CURSOR",
                    "message": (
                        "This output cursor is invalid for this process. Copy an "
                        "output_cursor from the same process, or omit since_cursor "
                        "to read its currently retained output."
                    ),
                }
            ),
        )


def _terminal_execution_result(
    call: ToolCall,
    result: TerminalResult,
    *,
    action: str = "start",
) -> ToolExecutionResult:
    payload = {
        "status": result.status.value,
        "terminal_process_action": action,
        "output": result.output,
        "exit_code": result.exit_code,
        "cwd": result.cwd,
        "timed_out": result.timed_out,
        "truncated": result.truncated,
        "error": result.error if action == "start" else None,
        "reason": None if result.reason is None else result.reason.value,
        "process_id": result.process_id,
        "yielded_to_background": result.status.value == "running",
        "output_disposition": result.output_disposition.value,
        "output_cursor": result.output_cursor,
        "retained_from_cursor": result.retained_from_cursor,
        "gap_before_output": result.gap_before_output,
        "truncated_by_response_bound": result.truncated_by_response_bound,
        "source_coverage": result.source_coverage.value,
    }
    state = (
        ToolResultState.SUCCESS
        if action != "start" or result.status.value in {"success", "running"}
        else ToolResultState.INTERRUPTED
        if result.status.value == "killed"
        else ToolResultState.ERROR
    )
    trusted_duration = (
        normalize_observation_duration(result.trusted_process_duration_microseconds)
        if action in {"start", "poll", "wait"}
        else None
    )
    return ToolExecutionResult(
        call_id=call.id,
        tool_name=call.name,
        status=state,
        output=json.dumps(payload, ensure_ascii=False),
        output_artifact_candidate=result.output_artifact_candidate,
        trusted_observation=(
            TrustedToolObservationSupplement(trusted_duration)
            if trusted_duration is not None
            else None
        ),
    )


def _validated_terminal_observation_supplement(
    *,
    tool: Tool,
    tool_name: str,
    arguments: Mapping[str, object],
    claimed: TrustedToolObservationSupplement | None,
) -> TrustedToolObservationSupplement | None:
    """Admit trusted duration only from the pinned Terminal binding matrix.

    ``ToolExecutionResult`` remains a neutral physical-result carrier, so a
    custom/builtin implementation can syntactically attach a supplement.  The
    Host tool owner must not trust it: only the exact Terminal executor binding
    and an observation action can promote that value into model-visible timing.
    Invalid claims are ignored so metadata cannot negate an already-known tool
    outcome.
    """

    if claimed is None:
        return None
    if tool_name == "terminal" and isinstance(tool, _DirectTerminalTool):
        return claimed
    if tool_name == "terminal_process" and isinstance(tool, _DirectTerminalProcessTool):
        action = parse_terminal_process_input(arguments).action
        if action in {"poll", "wait"}:
            return claimed
    return None


def _success(
    call: ToolCall,
    payload: Mapping[str, object],
    *,
    output_artifact_candidate: ToolOutputArtifactCandidate | None = None,
) -> ToolExecutionResult:
    return ToolExecutionResult(
        call_id=call.id,
        tool_name=call.name,
        status=ToolResultState.SUCCESS,
        output=json.dumps(dict(payload), ensure_ascii=False),
        output_artifact_candidate=output_artifact_candidate,
    )


def _terminal_monitor_rejected(call: ToolCall, reason: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        call_id=call.id,
        tool_name=call.name,
        status=ToolResultState.ERROR,
        output=json.dumps(
            {"status": "REJECTED", "reason": reason},
            ensure_ascii=False,
            sort_keys=True,
        ),
    )


def _workspace_relative_handoff_path(value: Path, root: Path) -> str:
    """Project a Terminal cwd without exposing a path outside the workspace."""

    resolved = value.expanduser().resolve()
    if resolved == root:
        return "."
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return "<outside-workspace>"


def _json_schema_value(value: Mapping[str, object]) -> dict[str, object]:
    """Lower recursively frozen catalog values to JSON Schema containers."""

    return {str(key): _thaw_json(item) for key, item in value.items()}


def _mcp_executor_capability_version(executor: McpBoundToolExecutor):
    semantic = executor.semantic
    source = capability_source_ref(
        CapabilitySourceKind.MCP_SERVER,
        semantic.server_id,
    )
    fact = freeze_tool_capability_fact(
        identity=capability_identity(
            kind=CapabilityKind.TOOL,
            source=source,
            stable_name=semantic.remote_tool_name,
        ),
        origin=ToolCapabilityOrigin.MCP,
        canonical_tool_spec=semantic.provider_spec(),
    )
    return tool_capability_version_ref(fact)


def _mcp_inspection_values(
    executor: McpBoundToolExecutor,
) -> McpInspectionDescriptorValues:
    semantic = executor.semantic
    return McpInspectionDescriptorValues(
        server_id=semantic.server_id,
        remote_tool_name=semantic.remote_tool_name,
        provider_tool_name=semantic.provider_tool_name,
        description=semantic.description,
        input_schema=semantic.input_schema,
        output_schema=semantic.output_schema,
        effect_kind=executor.policy.effect_kind.value,
    )


def _local_mcp_application_error(code: str) -> KernelToolResult:
    return KernelToolResult(
        state="APPLICATION_ERROR",
        content=canonical_json_bytes({"status": code}),
        effect_class="read_only",
    )


def _kernel_result_from_mcp_known(
    known: McpKnownToolResult,
    *,
    caller_cancelled: bool,
    effect_kind: McpEffectKind,
    physical_observation: PhysicalToolObservationSupplement,
) -> KernelToolResult:
    text = known.content.decode("utf-8")
    return KernelToolResult(
        state=known.state,
        content=known.content,
        remote_identity=known.remote_identity,
        output_artifact_candidate=ToolOutputArtifactCandidate(
            role="OUTPUT",
            text=text,
            source_coverage=ToolOutputSourceCoverage.COMPLETE,
            original_utf8_bytes=len(known.content),
            source_format_hint=ToolOutputSourceFormatHint.TEXT,
        ),
        caller_cancelled_while_running=caller_cancelled,
        effect_class=(
            "read_only"
            if effect_kind is McpEffectKind.READ_ONLY
            else "unknown_effect"
        ),
        physical_observation=physical_observation,
    )


def _freeze_physical_observation(
    started_at_monotonic: float,
    origin: ToolObservationOrigin,
) -> PhysicalToolObservationSupplement:
    elapsed = normalize_observation_duration(
        max(0, int((monotonic() - started_at_monotonic) * 1_000_000))
    )
    return PhysicalToolObservationSupplement(
        observed_at=datetime.now(timezone.utc),
        elapsed_microseconds=elapsed,
        observation_origin_kind=origin,
    )


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


__all__ = [
    "DirectKernelToolPort",
    "KernelToolInteractionPort",
    "ProductionBuiltinExecutorBinding",
    "production_builtin_executor_binding_identity_fingerprint",
]
