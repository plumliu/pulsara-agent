"""Production Host composition for the Stage 2 conversation kernel.

This composition deliberately has only canonical conversation, process-local
execution, selective journal, and exact-four background-job owners. Resume
acquires a new writer generation and rehydrates canonical rows only.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Awaitable, Callable, Mapping
from uuid import uuid4

from psycopg import IsolationLevel
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.direct_model import (
    DirectKernelModelPort,
    KernelModelExecutionRequest,
)
from pulsara_agent.conversation_kernel.context_sources import (
    KernelContextSourceCollector,
)
from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionConfirmationKind,
    CompactionDisposition,
    CompactionOutcome,
    CompactionScope,
    CompactionTrigger,
    PreparedManualCompactionCommand,
    build_prepared_manual_compaction_command,
)
from pulsara_agent.conversation_kernel.compaction.runtime import (
    HostCompactionRuntimeOwner,
    ManualCompactionRequest,
)
from pulsara_agent.conversation_kernel.cancellation import (
    ActiveTurnCancellationIntent,
    ForegroundCancellationCause,
)
from pulsara_agent.conversation_kernel.activation import (
    require_stage2_runtime_privilege_boundary,
)
from pulsara_agent.conversation_kernel.blob import (
    CanonicalContentPublisher,
    PostgresCanonicalBlobStore,
)
from pulsara_agent.conversation_kernel.capability import KernelSkillProjectionComposer
from pulsara_agent.capability.bundled_skills import (
    BundledSkillDistributionBindingOwner,
)
from pulsara_agent.capability.local_skills import LooseSkillDefinitionProducer
from pulsara_agent.capability.resolver import EffectiveSkillCatalogInspection
from pulsara_agent.capability.pulsara_home import (
    PulsaraHomeResolution,
    PulsaraHomeResolutionError,
    UserHomeResolution,
    resolve_pulsara_home,
    resolve_user_home,
)
from pulsara_agent.capability.user_skill_config import (
    USER_SKILL_CONFIG_NAME,
    load_user_skill_config,
    workspace_skill_config_path,
)
from pulsara_agent.capability.workspace_mcp_trust import (
    workspace_mcp_server_approvals,
)
from pulsara_agent.conversation_kernel.contracts import (
    ConversationScopeKind,
    PromptDeliveryMode,
    StoredCommittedEvent,
    WriterLease,
    canonical_digest,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    DEFAULT_KERNEL_WATCHDOG_POLICY,
    KernelExecutionDeadlineFactory,
    KernelExecutionWatchdogPolicy,
    KernelWatchdogOwner,
)
from pulsara_agent.conversation_kernel.input_continuity import (
    HostProviderInputContinuityOwner,
)
from pulsara_agent.conversation_kernel.assistant_settlement import (
    AssistantMessageSettlementOwner,
)
from pulsara_agent.conversation_kernel.auxiliary_model import (
    DirectKernelAuxiliaryJsonModel,
)
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.conversation_kernel.extensions import (
    ExtensionPlane,
    ExtensionPrincipal,
    ExtensionRegistrationLease,
    ExtensionRegistrationRequest,
    KernelExtensionHost,
    OperationalHookOffer,
    OperationalHookType,
    PostCommitHookOffer,
)
from pulsara_agent.conversation_kernel.interaction import KernelInteractionCoordinator
from pulsara_agent.conversation_kernel.plan_runtime import (
    ContinuationAdmissionOwner,
    KernelPlanInteractionCoordinator,
)
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.live_control import SessionLiveControlOwner
from pulsara_agent.conversation_kernel.memory_tools import KernelMemoryToolPort
from pulsara_agent.conversation_kernel.memory.governor import AdvisoryMemoryGovernor
from pulsara_agent.memory.scope import freeze_memory_read_context_binding
from pulsara_agent.conversation_kernel.query import CanonicalConversationQuery
from pulsara_agent.conversation_kernel.repository import (
    AcceptedPlanResolution,
    AcceptedPlanToolBatch,
    AcceptedPlanWorkflowCommand,
    ConversationKernelConflict,
    ConversationKernelRepository,
    SubagentCompletionDisposition,
    PromptIngressRejected,
    PlanContinuationDisposition,
    PlanQuestionAnswer,
    PlanContinuationInspection,
    PreparedPlanToolBatch,
    StaleHostWriter,
    plan_draft_review_semantic_candidate,
    plan_exit_semantic_fingerprint,
    plan_question_resolution_semantic_fingerprint,
)
from pulsara_agent.conversation_kernel.steer import (
    PreparedQueuedRootTurnAdmission,
    PreparedPromptIngressCommand,
    PromptIngressConfirmationKind,
    PromptIngressWriteRejection,
    PreparedRootTurnIdentity,
    QueuedRootTurnAdmissionConfirmation,
    QueuedRootTurnAdmissionConfirmationKind,
    build_direct_root_turn_identity,
    build_prompt_ingress_command,
    build_queued_root_turn_identity,
)
from pulsara_agent.conversation_kernel.runner import (
    ConversationKernelRunner,
    KernelRunResult,
)
from pulsara_agent.conversation_kernel.safe_point import ExternalSourceNotAtSafePoint
from pulsara_agent.conversation_kernel.subagent import KernelSubagentManager
from pulsara_agent.conversation_kernel.subagent import (
    SubagentCancelDisposition,
)
from pulsara_agent.conversation_kernel.user_control import (
    CONTROL_ADMISSION_WINDOW_MS,
    CONTROL_RESULT_RETENTION_MS,
    ControlQueryStatus,
    FeedbackCanonicalStatus,
    FeedbackInclusionStatus,
    FeedbackOwnerAvailability,
    FeedbackTransportResult,
    MonitorControlResult,
    ProcessControlResult,
    RootControlResult,
    SubagentControlResult,
    UserControlExecution,
    UserControlFeedbackState,
    UserControlOperation,
    UserControlOutcome,
    UserControlRequest,
    UserControlTarget,
    UserControlTargetKind,
    parse_control_command_deadline_ms,
)
from pulsara_agent.conversation_kernel.subagents.launch import (
    CanonicalSubagentLaunchPreparationPort,
)
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.conversation_kernel.todo_runtime import (
    FrozenTodoCloseProjection,
    FrozenTodoSnapshot,
    PreparedTodoChildRunActivation,
    PreparedTodoRootRunActivation,
    build_root_activation,
)
from pulsara_agent.conversation_kernel.tool_artifacts import (
    PostgresToolArtifactReadPort,
)
from pulsara_agent.conversation_kernel.tool_policy import (
    DefaultToolDispatchAuthorizationPolicy,
)
from pulsara_agent.workspace_identity import (
    HostWorkspaceInput,
    ResolvedWorkspace,
    resolve_workspace,
)
from pulsara_agent.llm.model_connections import (
    ModelCallBinding,
    model_call_binding_from_dict,
    model_call_binding_to_dict,
)
from pulsara_agent.llm.model_target import FrozenModelResolutionSnapshot
from pulsara_agent.llm.runtime import ModelRuntime, ModelRuntimeUnavailable
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.tool_permission import (
    EffectivePermissionPolicy,
    default_permission_policy,
    mode_for_policy,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot
from pulsara_agent.primitives.plan_workflow import PlanDraftDecision, PlanHandoffKind
from pulsara_agent.ports.system_prompt import DEFAULT_SYSTEM_PROMPT
from pulsara_agent.ports.terminal_observation import (
    ExistingTurnInstallation,
    NewTurnInstallation,
)
from pulsara_agent.ports.user_control_feedback import (
    UserControlFeedbackContentV1,
    UserControlFeedbackInstallationAttempt,
    UserControlMonitorFact,
    UserControlProcessFact,
    project_user_control_feedback_for_provider,
)
from pulsara_agent.terminal_process.models import TerminalProcessInfo
from pulsara_agent.mcp_config import McpServerConfig
from pulsara_agent.capability.mcp_management import LocalMcpManagementService
from pulsara_agent.conversation_kernel.mcp import McpHostSupervisor
from pulsara_agent.conversation_kernel.mcp.contracts import (
    McpCatalogSnapshot,
    McpConfiguredServerInspection,
)
from pulsara_agent.retrieval.config import RetrievalConfig
from pulsara_agent.hooks.context import (
    HookContextOwner,
    PendingHookContextReservation,
)
from pulsara_agent.hooks.contracts import (
    DirectPromptRef,
    FrozenHookDefinitionView,
    GateDecision,
    HookDispatchEnvelope,
    HookDispatchScopeRef,
    HookScopeKind,
    HookSourceKind,
    QueuedPromptRef,
    SessionEndInput,
    SessionEndRef,
    UserPromptSubmitInput,
    external_permission_mode,
)
from pulsara_agent.hooks.dispatcher import (
    KernelHookDispatcher,
    LoggingHookDiagnosticAdapter,
)
from pulsara_agent.hooks.executor import HookSecretScrubSet
from pulsara_agent.hooks.source import LocalHookSourceProvider
from pulsara_agent.hooks.matcher import event_matcher_subject
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.storage.schema_verification_service import (
    VerifiedPostgresAccessLease,
    process_postgres_schema_verification_service,
)
from pulsara_agent.plugins.contracts import (
    EnabledPluginViewDisposition,
    ExternalProcessAcceptance,
    InspectLocalPluginsRequest,
    InstallLocalPluginRequest,
    NeverCancelPluginOperation,
    PluginDiagnosticCode,
    PluginEnablementOutcome,
    PluginInspectionResult,
    PluginInstallOutcome,
    PluginMcpNormalizationDisposition,
    PluginRemovalOutcome,
    PluginScopeKind,
    RemoveLocalPluginRequest,
    SetLocalPluginEnabledRequest,
)
from pulsara_agent.plugins.hook_adapter import compose_hook_definition_view
from pulsara_agent.plugins.management import PluginManagementService
from pulsara_agent.plugins.mcp_adapter import normalize_plugin_mcp_configs
from pulsara_agent.plugins.package_store import ManagedPluginStore
from pulsara_agent.plugins.skill_producer import PluginSkillDefinitionProducer
from pulsara_agent.plugins.view import (
    EnabledPluginViewOwner,
    FrozenEnabledPluginView,
)
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary


MAXIMUM_PROMPT_BYTES = STAGE2_LIMITS.prompt_hard_bytes


async def _shielded_plugin_filesystem_call(operation, /, **kwargs):
    """Join a started Plugin filesystem worker before cancellation escapes."""

    task = asyncio.create_task(
        asyncio.to_thread(operation, **kwargs),
        name=f"plugin-filesystem:{getattr(operation, '__name__', 'operation')}",
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        # A settled physical cause intentionally wins here; otherwise close
        # the successfully returned RAII value before propagating cancellation.
        result = task.result()
        close = getattr(result, "close", None)
        if close is not None:
            close()
        raise


@dataclass(frozen=True, slots=True)
class KernelSessionSummary:
    session_id: str
    workspace_id: str
    workspace_kind: str
    workspace_root: str
    workspace_label: str
    memory_domain_id: str
    lifecycle: str
    writer_generation: int
    latest_entry_sequence: int
    updated_at: datetime
    model_call_binding: ModelCallBinding | None = None
    subagent_task_total: int = 0
    subagent_task_active: int = 0
    subagent_task_waiting: int = 0
    subagent_task_attention: int = 0

    @property
    def runtime_session_id(self) -> str:
        return self.session_id

    def to_dict(self) -> dict[str, object]:
        return {
            "runtime_session_id": self.session_id,
            "workspace_id": self.workspace_id,
            "workspace_kind": self.workspace_kind,
            "workspace_root": self.workspace_root,
            "workspace_label": self.workspace_label,
            "memory_domain_id": self.memory_domain_id,
            "lifecycle": self.lifecycle,
            "writer_generation": self.writer_generation,
            "latest_entry_sequence": self.latest_entry_sequence,
            "updated_at": self.updated_at.isoformat(),
            "model_call_binding": model_call_binding_to_dict(self.model_call_binding),
        }


@dataclass(frozen=True, slots=True)
class KernelPromptDelivery:
    queue_item_id: str
    queue_status: str
    consumed_entry_id: str | None
    delivery_mode: str


@dataclass(frozen=True, slots=True)
class KernelCommandOutcome:
    command_id: str
    status: str
    target_id: str
    public_code: str
    public_message: str
    plan_workflow_status: str | None = None
    resume_permission_mode: PermissionMode | None = None
    handoff_created_at_commit: bool = False
    plan_workflow_revision: int | None = None
    plan_draft_decision: PlanDraftDecision | None = None
    plan_continuation_turn_id: str | None = None
    prompt_delivery: KernelPromptDelivery | None = None
    user_control: UserControlOutcome | None = None


@dataclass(frozen=True, slots=True)
class KernelControlQueryResult:
    status: ControlQueryStatus
    outcome: KernelCommandOutcome | None


@dataclass(slots=True)
class _UserControlAttempt:
    request: UserControlRequest
    outcome: KernelCommandOutcome
    task: asyncio.Task[None] | None = None
    finished_monotonic_ms: int | None = None
    retain_until_monotonic_ms: int | None = None
    feedback_target_turn_id: str | None = None
    feedback_candidate: UserControlFeedbackInstallationAttempt | None = None
    feedback_provider_text: str | None = None
    feedback_candidate_ready: asyncio.Event = field(default_factory=asyncio.Event)
    feedback_canonical_settled: asyncio.Event = field(default_factory=asyncio.Event)
    feedback_install_attempted: bool = False
    feedback_commit_outcome_unknown: bool = False
    normal_end_coordination_fenced: bool = False
    normal_end_coordination_exhausted: bool = False
    normal_end_coordination_deadline: float | None = None
    provider_input_observations: set[tuple[str, str, int]] = field(default_factory=set)
    transport_observations: dict[tuple[str, str, int], FeedbackTransportResult] = field(
        default_factory=dict
    )
    process_at_admission: TerminalProcessInfo | None = None


@dataclass(frozen=True, slots=True)
class KernelMcpToolCatalogItem:
    """Renderer-neutral public facts for one currently discovered MCP tool."""

    server_id: str
    remote_name: str
    provider_name: str
    description: str
    effect: str
    available_to_subagents: bool
    parallel_safe: bool


@dataclass(frozen=True, slots=True)
class KernelCapabilityCatalogInspection:
    """Disposable product projection frozen directly from capability owners."""

    mcp_catalog: McpCatalogSnapshot
    mcp_configured_servers: tuple[McpConfiguredServerInspection, ...]
    mcp_tools: tuple[KernelMcpToolCatalogItem, ...]
    skill_catalog: EffectiveSkillCatalogInspection
    configured_active_skill_names: frozenset[str]


class PromptBlockedByHook(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class _IngressHookApiVariant(StrEnum):
    DIRECT = "DIRECT"
    QUEUED = "QUEUED"


@dataclass(frozen=True, slots=True)
class _IngressHookReservationKey:
    api_variant: _IngressHookApiVariant
    command_id: str
    prompt_utf8: bytes
    requested_permission_mode: PermissionMode | None
    queue_item_id: str | None


@dataclass(slots=True)
class _IngressHookAttempt:
    key: _IngressHookReservationKey
    future: asyncio.Future[KernelRunResult | KernelCommandOutcome]


class RootChainPhase(StrEnum):
    ORIGIN_RUNNING = "ORIGIN_RUNNING"
    SUCCESSOR_COMMITTED_PENDING_HANDOFF = "SUCCESSOR_COMMITTED_PENDING_HANDOFF"
    SUCCESSOR_RUNNING = "SUCCESSOR_RUNNING"


@dataclass(frozen=True, slots=True)
class _PendingRootSuccessorHandoff:
    owner_task: asyncio.Task[KernelRunResult]
    attempt_id: str
    origin_turn_id: str
    successor_turn_id: str
    successor_entry_id: str
    workflow_id: str
    interaction_id: str | None
    handoff_kind: PlanHandoffKind


class KernelCompositionUnavailable(ValueError):
    pass


class HostSessionCloseState(StrEnum):
    TASK_INSTALLED = "TASK_INSTALLED"
    CLOSED = "CLOSED"
    CLOSE_FAILED_QUARANTINED = "CLOSE_FAILED_QUARANTINED"


class HostSessionCloseDecisionFrozen(RuntimeError):
    """A canonical-close upgrade arrived after its linearization fence."""


class KernelHostCoreClosing(RuntimeError):
    """A new Host session was rejected after process shutdown won admission."""


@dataclass(slots=True)
class HostSessionCloseAttempt:
    """Unique process-local close owner installed by ``KernelHostCore``."""

    host_session_id: str
    session: "KernelHostSession"
    deadline_monotonic: float
    close_conversation_requested: bool
    task: asyncio.Task[None]
    state: HostSessionCloseState = HostSessionCloseState.TASK_INSTALLED
    close_decision_frozen: bool = False

    def merge_close_conversation(self, requested: bool) -> None:
        if requested and not self.close_conversation_requested:
            if self.close_decision_frozen:
                raise HostSessionCloseDecisionFrozen(
                    "canonical close decision is already frozen"
                )
            if self.state is not HostSessionCloseState.TASK_INSTALLED:
                raise RuntimeError(
                    "canonical close cannot be added after the Host close settled"
                )
            self.close_conversation_requested = True
            self.session.request_close_conversation()

    def freeze_close_conversation(self) -> bool:
        if self.close_decision_frozen:
            raise RuntimeError("canonical close decision was frozen twice")
        self.close_decision_frozen = True
        return self.close_conversation_requested


class KernelHostSession:
    def __init__(
        self,
        *,
        model_runtime: ModelRuntime,
        workspace: ResolvedWorkspace,
        repository: ConversationKernelRepository,
        writer_lease: WriterLease,
        io_owner: KernelSessionIO,
        session_id: str,
        host_session_id: str,
        permission_policy: EffectivePermissionPolicy,
        system_prompt: str | None,
        active_skill_names: frozenset[str],
        authenticated_first_party_extension_ids: frozenset[str],
        deadline_factory: KernelExecutionDeadlineFactory,
        session_start_source: str,
        hook_source_provider: LocalHookSourceProvider,
        initial_hook_view: FrozenHookDefinitionView,
        bundled_skill_binding: BundledSkillDistributionBindingOwner,
        pulsara_home_resolution: PulsaraHomeResolution,
        user_home_resolution: UserHomeResolution,
        plugin_view_owner: EnabledPluginViewOwner,
        initial_plugin_view: FrozenEnabledPluginView,
        credential_boundary: ProcessCredentialBoundary,
        mcp_management: LocalMcpManagementService,
        capability_change_notifier: Callable[[Path | None], Awaitable[int]]
        | None = None,
        local_mcp_configs: tuple[McpServerConfig, ...] = (),
        mcp_configs: tuple[McpServerConfig, ...] = (),
        control_monotonic: Callable[[], float] = monotonic,
    ) -> None:
        self._model_runtime = model_runtime
        self.mcp_management = mcp_management
        self._capability_change_notifier = capability_change_notifier
        self.workspace = workspace
        self.repository = repository
        self.runtime_session_id = session_id
        self.session_id = session_id
        self.host_session_id = host_session_id
        self.live_bus = LiveAgentEventBus()
        self.extensions = KernelExtensionHost(
            session_id=session_id,
            authenticated_first_party_principal_ids=(
                authenticated_first_party_extension_ids
            ),
        )
        self.live_bus.bind_extension_tap(self.extensions.offer_live_nowait)
        self.query = CanonicalConversationQuery(
            repository.connection_provider,
            watchdog_policy=deadline_factory.policy,
        )
        self._content_publisher = CanonicalContentPublisher(
            repository.connection_provider
        )
        self._io = io_owner
        self._deadlines = deadline_factory
        self._control_monotonic = control_monotonic
        self._lease = writer_lease
        self._memory_domain_id = workspace.memory_domain.memory_domain_id
        self.live_control = SessionLiveControlOwner(
            session_id=session_id,
            owner_epoch=self._lease.guard.writer_generation,
        )
        self._interactions = KernelInteractionCoordinator(
            repository=repository,
            guard=self._lease.guard,
            live_control=self.live_control,
            live_bus=self.live_bus,
            io_owner=self._io,
            deadline_factory=self._deadlines,
        )
        self._presentation_notices: dict[str, list[str]] = {}
        self._plan_interactions = KernelPlanInteractionCoordinator()
        self._plan_continuations = ContinuationAdmissionOwner()
        self._input_continuity = HostProviderInputContinuityOwner(session_id=session_id)
        self._compaction = HostCompactionRuntimeOwner()
        self._assistant_settlements = AssistantMessageSettlementOwner(
            repository=repository,
            io_owner=self._io,
            continuity_owner=self._input_continuity,
            deadline_factory=self._deadlines,
        )
        self._event_loop = asyncio.get_running_loop()
        launch_permission_mode = mode_for_policy(permission_policy)
        if launch_permission_mode is None:
            raise KernelCompositionUnavailable(
                "production Host permission must be a closed preset"
            )
        self._launch_permission_mode = launch_permission_mode
        self._monitor_wake = asyncio.Event()
        self._hook_diagnostics = LoggingHookDiagnosticAdapter()
        self._hook_context = HookContextOwner(self._hook_diagnostics)
        self._hook_root_scope = HookDispatchScopeRef(
            self,
            workspace,
            HookScopeKind.ROOT,
        )
        self._hook_context.register_scope(self._hook_root_scope)
        self._hook_source_provider = hook_source_provider
        self._hooks = KernelHookDispatcher(
            initial_view=initial_hook_view,
            workspace_root=workspace.workspace_root,
            source_provider=self._hook_source_provider,
            credential_boundary=credential_boundary,
            diagnostic_adapter=self._hook_diagnostics,
            background_context=self._hook_context,
        )
        self._plugin_view_owner = plugin_view_owner
        self._plugin_view = initial_plugin_view
        self._plugin_skill_producer = PluginSkillDefinitionProducer()
        self._plugin_skill_definitions = self._plugin_skill_producer.observe(
            initial_plugin_view
        )
        self._local_mcp_configs = local_mcp_configs

        def wake_terminal_monitor_scheduler() -> None:
            self._event_loop.call_soon_threadsafe(self._monitor_wake.set)

        self._tools = DirectKernelToolPort(
            workspace_root=workspace.workspace_root,
            host_owner_id=host_session_id,
            session_id=session_id,
            live_bus=self.live_bus,
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
            artifact_read_port=PostgresToolArtifactReadPort(
                repository.connection_provider,
                session_id=session_id,
                workspace_id=workspace.workspace_key,
            ),
            terminal_monitor_wake_scheduler=wake_terminal_monitor_scheduler,
            deadline_factory=self._deadlines,
            pulsara_home_resolution=pulsara_home_resolution,
            user_home_resolution=user_home_resolution,
        )
        self._tools.bind_interaction_port(self._interactions)
        self._tools.bind_capability_reload_port(self)
        from .capability_management import CapabilityManagementPreparation

        self._tools.bind_capability_management(
            CapabilityManagementPreparation(
                mcp=mcp_management,
                plugins=PluginManagementService(
                    credential_boundary=credential_boundary,
                    pulsara_home_resolution=pulsara_home_resolution,
                ),
                workspace_root=workspace.workspace_root,
                deadline=lambda: self._deadlines.deadline(
                    KernelWatchdogOwner.NONTERMINAL_TOOL_INVOCATION
                ),
            )
        )
        self._subagents = KernelSubagentManager(
            repository=repository,
            guard=self._lease.guard,
            host_owner_id=host_session_id,
            io_owner=self._io,
            live_bus=self.live_bus,
            todo_owner=self._tools.todo_owner,
            launch_preparation=CanonicalSubagentLaunchPreparationPort(
                repository=repository,
                guard=self._lease.guard,
                io_owner=self._io,
                model_runtime=model_runtime,
                deadline_factory=self._deadlines,
            ),
            terminal_cwd=self._tools.snapshot_terminal_cwd,
            todo_close_projector=self._tools.offer_todo_close,
            deadline_factory=self._deadlines,
            hook_dispatcher=self._hooks,
            hook_context_owner=self._hook_context,
            hook_root_scope=self._hook_root_scope,
        )
        self._tools.bind_subagent_port(self._subagents)
        memory_read_binding = freeze_memory_read_context_binding(
            domain=workspace.memory_domain,
            host_workspace_id=workspace.workspace_key,
        )
        retrieval = RetrievalConfig()
        self._memory_tools = KernelMemoryToolPort(
            repository=repository,
            session_id=session_id,
            read_binding=memory_read_binding,
            embedding_config=retrieval.embedding,
            rerank_config=retrieval.rerank,
            feature_config=retrieval.memory,
            io_owner=self._io,
            settings=model_runtime.settings,
        )
        self._memory_tools.bind_deadline_factory(self._deadlines)
        self._memory_governor = AdvisoryMemoryGovernor(
            repository=repository,
            guard=self._lease.guard,
            read_binding=memory_read_binding,
            model=DirectKernelAuxiliaryJsonModel(model_runtime),
            input_reader=CanonicalProviderInputReader(
                repository.connection_provider,
                blob_reader=PostgresCanonicalBlobStore(repository.connection_provider),
            ),
            io_owner=self._io,
            deadline_factory=self._deadlines,
            embedding_port=self._memory_tools,
        )
        self._memory_tools.bind_governor(self._memory_governor)
        self._tools.bind_memory_port(self._memory_tools)
        self._mcp_supervisor = McpHostSupervisor(
            session_id=session_id,
            workspace_root=workspace.workspace_root,
            configs=mcp_configs,
            credential_boundary=credential_boundary,
        )
        self._tools.bind_mcp_supervisor(self._mcp_supervisor)
        self._tools.seal_builtin_composition()
        pulsara_home_path = pulsara_home_resolution.path
        if pulsara_home_path is None:
            raise PulsaraHomeResolutionError(pulsara_home_resolution)
        user_skill_config_path = pulsara_home_path / USER_SKILL_CONFIG_NAME
        self._capabilities = KernelSkillProjectionComposer(
            workspace_root=workspace.workspace_root,
            bundled_binding_owner=bundled_skill_binding,
            plugin_definitions_provider=lambda: self._plugin_skill_definitions,
            configured_active_skill_names=active_skill_names,
            user_skill_config_provider=lambda: load_user_skill_config(
                config_path=user_skill_config_path
            ),
            workspace_skill_config_provider=lambda: load_user_skill_config(
                config_path=workspace_skill_config_path(self.workspace.workspace_root)
            ),
            loose_producer=LooseSkillDefinitionProducer(
                pulsara_home_resolution=pulsara_home_resolution,
                user_home_resolution=user_home_resolution,
            ),
        )
        self._model = DirectKernelModelPort(
            model_runtime=model_runtime,
            usage_observer=self._observe_provider_usage,
            timeout_policy=self._deadlines.policy.foreground_transport,
            transport_invocation_observer=(
                self._observe_user_control_transport_invocation
            ),
        )
        display_timezone = datetime.now().astimezone().tzinfo
        if display_timezone is None:
            raise RuntimeError("Host display timezone is unavailable")
        self._context_sources = KernelContextSourceCollector(
            workspace_kind=workspace.workspace_kind,
            workspace_root=workspace.workspace_root,
            terminal_cwd=self._tools,
            capability_composer=self._capabilities,
            base_system_prompt=system_prompt or DEFAULT_SYSTEM_PROMPT,
            display_timezone=display_timezone,
            mcp_catalog=self._mcp_supervisor,
            hook_context_owner=self._hook_context,
        )
        self._runner = ConversationKernelRunner(
            repository=repository,
            writer_lease=self._lease,
            model=self._model,
            tools=self._tools,
            live_bus=self.live_bus,
            before_provider_preparation=self._adopt_capabilities_if_requested,
            root_control_preparation_barrier=(
                self._await_user_control_feedback_before_root_provider
            ),
            root_control_completion_fence=(
                self._coordinate_user_control_feedback_at_root_completion
            ),
            root_control_completion_settlement=(
                self._settle_user_control_feedback_root_completion
            ),
            io_owner=self._io,
            context_source_collector=self._context_sources,
            model_resolution_snapshot_provider=(
                self._model_runtime.freeze_resolution_snapshot
            ),
            continuity_owner=self._input_continuity,
            extensions=self.extensions,
            workspace_id=workspace.workspace_key,
            plan_interactions=self._plan_interactions,
            automatic_plan_continuation=(self._accept_automatic_plan_continuation),
            launch_permission_mode=self._launch_permission_mode,
            deadline_factory=self._deadlines,
            memory_projection=self._memory_tools,
            assistant_settlement_owner=self._assistant_settlements,
            todo_admission_finalizer=self._finalize_todo_run_activation,
            compaction_owner=self._compaction,
            subagent_runtime=self._subagents,
            hook_dispatcher=self._hooks,
            hook_context_owner=self._hook_context,
            hook_scope=self._hook_root_scope,
            session_start_source=session_start_source,
            presentation_notice_sink=self._offer_presentation_notice,
            provider_input_installed_observer=(
                self._observe_user_control_provider_input_install
            ),
            provider_input_failure_observer=(
                self._record_user_control_provider_input_failure
            ),
        )
        self._subagents.bind_runner_factory(self._new_child_runner)
        self._active_task: asyncio.Task[KernelRunResult] | None = None
        self._active_turn_id: str | None = None
        self._active_cancellation_intent: ActiveTurnCancellationIntent | None = None
        self._active_command_id: str | None = None
        self._active_root_phase: RootChainPhase | None = None
        self._pending_root_successor: _PendingRootSuccessorHandoff | None = None
        self._control_completion_sealed_turn_id: str | None = None
        self._external_new_turn_accepting = False
        self._plan_exit_fence = False
        self._terminal_new_turn_observation_id: str | None = None
        self._external_new_turn_settled = asyncio.Event()
        self._external_new_turn_settled.set()
        self._command_failures: dict[str, KernelCommandOutcome] = {}
        self._user_control_attempts: dict[str, _UserControlAttempt] = {}
        self._lock = asyncio.Lock()
        self._capability_reload_settlement_lock = asyncio.Lock()
        self._capability_refresh_requested_revision = 0
        self._capability_refresh_applied_revision = 0
        self._capability_refresh_attention: str | None = None
        self._ingress_hook_attempts: dict[str, _IngressHookAttempt] = {}
        self._compaction_write_reservations: dict[
            tuple[ModelInputScopeKind, str | None], int
        ] = {}
        self._manual_compaction_command_attempts: dict[
            str, tuple[str, asyncio.Task[CompactionConfirmationKind]]
        ] = {}
        self._compaction.bind_host_fence_callbacks(
            install=self._install_compaction_fence,
            remove=self._remove_compaction_fence,
        )
        self._close_async_lock = asyncio.Lock()
        self._closing = False
        self._closed = False
        self._close_conversation_requested = False
        self._queue_wake = asyncio.Event()
        self._renewal_task = asyncio.create_task(
            self._renew_writer(), name=f"kernel-writer-renew:{session_id}"
        )
        self._delivery_task = asyncio.create_task(
            self._prompt_delivery_loop(),
            name=f"kernel-prompt-delivery:{session_id}",
        )
        self._memory_governor.start()
        self._monitor_task = asyncio.create_task(
            self._terminal_monitor_delivery_loop(),
            name=f"kernel-terminal-monitor-delivery:{session_id}",
        )
        self._queue_wake.set()

    async def _install_compaction_fence(
        self,
        scope: CompactionScope,
        trigger: CompactionTrigger,
        attempt_id: str,
        owner_task: asyncio.Task[object],
    ) -> None:
        """Recapture and install one exact-scope fence under the Host lock."""

        async with self._lock:
            self._require_open()
            key = self._compaction.scope_key(
                scope.scope_kind, scope.scope_subagent_task_id
            )
            if self._compaction_write_reservations.get(key, 0):
                raise RuntimeError("compaction target has an admitted writer")
            if scope.scope_kind is ModelInputScopeKind.ROOT:
                self._retire_done_active_root_locked()
                active_owner = self._active_task is owner_task
                idle_owner = self._active_task is None
                if not (active_owner or idle_owner):
                    raise RuntimeError("compaction ROOT task ownership changed")
                if active_owner and self._active_turn_id != scope.turn_id:
                    raise RuntimeError("compaction active ROOT target changed")
                if idle_owner and (
                    self._external_new_turn_accepting
                    or self._pending_root_successor is not None
                ):
                    raise RuntimeError("compaction idle ROOT admission is busy")
                if self._plan_exit_fence:
                    raise RuntimeError("compaction conflicts with Plan force exit")
            else:
                task_id = scope.scope_subagent_task_id
                if task_id is None or not self._subagents.owns_active_compaction_target(
                    task_id=task_id,
                    turn_id=scope.turn_id,
                    owner_task=owner_task,
                ):
                    raise RuntimeError("compaction child task ownership changed")
            self._compaction.install_fence_under_host_lock(
                scope=scope,
                trigger=trigger,
                attempt_id=attempt_id,
                owner_task=owner_task,
            )

    async def _remove_compaction_fence(
        self,
        scope: CompactionScope,
        attempt_id: str,
        owner_task: asyncio.Task[object],
    ) -> None:
        async with self._lock:
            self._compaction.remove_fence_under_host_lock(
                scope=scope,
                attempt_id=attempt_id,
                owner_task=owner_task,
            )
            self._queue_wake.set()
            self._monitor_wake.set()

    def _reserve_compaction_write_locked(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
    ) -> tuple[ModelInputScopeKind, str | None]:
        key = self._compaction.scope_key(scope_kind, scope_subagent_task_id)
        if self._compaction.is_fenced(
            scope_kind=scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
        ):
            raise RuntimeError("COMPACTION_IN_PROGRESS")
        self._compaction_write_reservations[key] = (
            self._compaction_write_reservations.get(key, 0) + 1
        )
        return key

    async def _release_compaction_write_reservation(
        self, key: tuple[ModelInputScopeKind, str | None]
    ) -> None:
        async with self._lock:
            count = self._compaction_write_reservations.get(key, 0)
            if count <= 1:
                self._compaction_write_reservations.pop(key, None)
            else:
                self._compaction_write_reservations[key] = count - 1

    async def start_mcp(self) -> None:
        await self._mcp_supervisor.start()
        self._tools.prepare_tool_surface_safe_point()

    async def reload_mcp_configs(
        self,
        configs: tuple[McpServerConfig, ...],
        *,
        deadline_monotonic: float,
    ) -> frozenset[str]:
        """Install a process-local config epoch; publication waits for safe point."""

        self._require_open()
        return await self._tools.reload_mcp_configs(
            configs, deadline_monotonic=deadline_monotonic
        )

    async def reload_hooks(
        self, *, deadline_monotonic: float | None
    ) -> dict[str, object]:
        """Replace only the future-event USER/WORKSPACE Hook source slices."""

        async def publish_scanned_view(predecessor, replacement) -> bool:
            async with self._lock:
                if self._closing or self._closed:
                    return False
                return self._hooks.publish_scanned_view(predecessor, replacement)

        view = await self._hooks.reload(
            deadline_monotonic=deadline_monotonic,
            publish_scanned_view=publish_scanned_view,
        )
        result = {
            "status": "RELOADED",
            "sources": [
                {
                    "source": snapshot.provenance.identity.kind.value,
                    "path": str(snapshot.provenance.identity.canonical_path),
                    "scan": snapshot.disposition.value,
                    "trust": snapshot.trust.disposition.value,
                    "enabled": snapshot.trust.enabled,
                    "definitions": len(snapshot.definitions),
                }
                for snapshot in view.source_snapshots
            ],
        }
        safe = HookSecretScrubSet.capture().scrub_json(result)
        if not isinstance(safe, dict):
            raise RuntimeError("Hook reload result lost its JSON object shape")
        return safe

    async def reload_capabilities(
        self, *, deadline_monotonic: float | None
    ) -> dict[str, object]:
        """Reload local MCP and Plugin sources without rebasing provider input."""

        self._require_open()
        deadline = (
            self._deadlines.deadline(KernelWatchdogOwner.NONTERMINAL_TOOL_INVOCATION)
            if deadline_monotonic is None
            else deadline_monotonic
        )
        # Scans/builds below never hold the Hook publication or Host lock.  The
        # settlement mutex only prevents a later Capability reload's native MCP cut
        # from overtaking an earlier Hook/Skill publication.
        await _acquire_lock_before_deadline(
            self._capability_reload_settlement_lock,
            deadline,
            "Capability reload settlement deadline expired",
        )
        try:
            local_mcp_configs = await asyncio.to_thread(
                self.mcp_management.load_configs,
                workspace_root=self.workspace.workspace_root,
                trust_workspace_config=self.workspace.trust_workspace_mcp_config,
                approved_workspace_server_identities=(
                    workspace_mcp_server_approvals(self.workspace.workspace_root)
                ),
            )
            predecessor_local_mcp_configs = self._local_mcp_configs
            self._local_mcp_configs = local_mcp_configs
            try:
                return await self._reload_capabilities_serialized(deadline)
            except BaseException:
                self._local_mcp_configs = predecessor_local_mcp_configs
                raise
        finally:
            self._capability_reload_settlement_lock.release()

    async def adopt_capability_management_change(self, *, workspace_root: Path | None):
        pending = 0
        if self._capability_change_notifier is not None:
            pending = await self._capability_change_notifier(workspace_root)
        await self.request_capability_refresh()
        await self._adopt_capabilities_if_requested()
        attention = bool(self.capability_refresh_attention)
        return {
            "status": "PARTIAL" if attention else "RELOADED",
            "reloaded_sessions": int(not attention),
            "pending_sessions": max(0, pending - 1),
            "attention_sessions": int(attention),
        }

    async def request_capability_refresh(self) -> int:
        """Mark the next unprepared dispatch; do not wake an otherwise idle turn."""

        async with self._lock:
            self._require_open()
            self._capability_refresh_requested_revision += 1
            revision = self._capability_refresh_requested_revision
            return revision

    @property
    def capability_refresh_pending(self) -> bool:
        return (
            self._capability_refresh_requested_revision
            > self._capability_refresh_applied_revision
        )

    @property
    def capability_refresh_attention(self) -> str | None:
        return self._capability_refresh_attention

    async def _adopt_capabilities_if_requested(self) -> bool:
        """Adopt marked user/directory sources before the next provider preparation."""

        async with self._lock:
            requested = self._capability_refresh_requested_revision
            if requested <= self._capability_refresh_applied_revision:
                return True
        attention: str | None = None
        try:
            outcome = await self.reload_capabilities(deadline_monotonic=None)
        except asyncio.CancelledError:
            raise
        except Exception:
            attention = "PROJECT_CAPABILITY_ADOPTION_FAILED"
        else:
            if outcome.get("mcp") != "RELOADED":
                attention = "PROJECT_MCP_ADOPTION_INCOMPLETE"
        async with self._lock:
            self._capability_refresh_applied_revision = max(
                self._capability_refresh_applied_revision,
                requested,
            )
            self._capability_refresh_attention = attention
        return True

    async def _reload_capabilities_serialized(
        self, deadline: float
    ) -> dict[str, object]:
        predecessor = self._plugin_view
        replacement = await _shielded_plugin_filesystem_call(
            self._plugin_view_owner.observe,
            workspace_root=(
                self.workspace.workspace_root
                if self.workspace.workspace_kind == "project"
                else None
            ),
            deadline_monotonic=deadline,
            cancellation=NeverCancelPluginOperation(),
        )
        try:
            _raise_if_deadline_expired(
                deadline, "Capability reload candidate deadline expired"
            )
            skill_definitions = self._plugin_skill_producer.observe(replacement)
            plugin_only_hooks = compose_hook_definition_view(
                local_view=FrozenHookDefinitionView(()),
                plugin_view=replacement,
                trust_store=self._hook_source_provider.trust_store,
            ).source_snapshots
            mcp = normalize_plugin_mcp_configs(
                existing_configs=self._local_mcp_configs,
                view=replacement,
                secret_resolver=self.mcp_management.settings.read().mcp_secret,
                oauth_manager=self.mcp_management.oauth,
                current_state=self._plugin_view_owner.current_state,
            )
            _raise_if_deadline_expired(
                deadline, "Capability reload composition deadline expired"
            )
        except BaseException:
            replacement.close()
            raise

        async def publish_scanned_view(hook_predecessor, hook_replacement) -> bool:
            await _acquire_lock_before_deadline(
                self._lock,
                deadline,
                "Capability reload Host publication deadline expired",
            )
            try:
                _raise_if_deadline_expired(
                    deadline, "Capability reload Host publication deadline expired"
                )
                if (
                    self._closing
                    or self._closed
                    or self._plugin_view is not predecessor
                ):
                    return False
                if not self._hooks.publish_scanned_view(
                    hook_predecessor, hook_replacement
                ):
                    return False
                self._plugin_view = replacement
                self._plugin_skill_definitions = skill_definitions
                return True
            finally:
                self._lock.release()

        try:
            hook_view = await self._hooks.publish_plugin_slice(
                plugin_snapshots=plugin_only_hooks,
                deadline_monotonic=deadline,
                publish_scanned_view=publish_scanned_view,
            )
        except BaseException:
            mcp.close_plugin_anchors()
            replacement.close()
            raise

        predecessor.close()
        mcp_status = (
            "UNAVAILABLE"
            if mcp.disposition is PluginMcpNormalizationDisposition.UNAVAILABLE
            else "BOUND_EXCEEDED"
        )
        changed: frozenset[str] = frozenset()
        if not mcp.configured_bound_exceeded and monotonic() < deadline:
            reload_task = asyncio.create_task(
                self._tools.reload_mcp_configs(
                    mcp.configs, deadline_monotonic=deadline
                ),
                name="plugin-mcp-config-reload",
            )
            try:
                changed = await asyncio.wait_for(
                    asyncio.shield(reload_task),
                    timeout=max(0.0, deadline - monotonic()),
                )
                mcp_status = (
                    "UNAVAILABLE"
                    if mcp.disposition is PluginMcpNormalizationDisposition.UNAVAILABLE
                    else "RELOADED"
                )
            except asyncio.CancelledError:
                # The native config cut occurs before its asynchronous
                # confirmation cleanup.  Join that exact owner so caller
                # cancellation cannot strand a half-settled cut or close
                # anchors already adopted by the supervisor.
                with suppress(BaseException):
                    await asyncio.shield(reload_task)
                raise
            except TimeoutError:
                # The native owner decides whether its synchronous config cut
                # became FULL.  Join its exact confirmation/cleanup owner, but
                # retain the caller's logical timeout as a partial reload.
                with suppress(BaseException):
                    await asyncio.shield(reload_task)
                mcp_status = "PARTIAL"
            except BaseException:
                # Hook/Skill publication is already FULL and has no cross-owner
                # rollback.  The native MCP owner preserves its own exact
                # predecessor on failure.
                mcp_status = "PARTIAL"
        else:
            # No native owner adopted these duplicated package anchors.
            mcp.close_plugin_anchors()
        reload_diagnostics = [item.code.value for item in mcp.diagnostics]
        if mcp_status != "RELOADED":
            reload_diagnostics.append(PluginDiagnosticCode.RELOAD_PARTIAL.value)
        result = {
            "status": "RELOADED" if mcp_status == "RELOADED" else "PARTIAL",
            "plugin_view": replacement.disposition.value,
            "skill_producer": skill_definitions.disposition.value,
            "hook_sources": len(
                tuple(
                    item
                    for item in hook_view.source_snapshots
                    if item.provenance.identity.kind is HookSourceKind.PLUGIN
                )
            ),
            "mcp": mcp_status,
            "mcp_changed_server_ids": sorted(changed),
            "diagnostics": reload_diagnostics,
            "same_epoch_prefix": "UNCHANGED",
        }
        safe = HookSecretScrubSet.capture().scrub_json(result)
        if not isinstance(safe, dict):
            raise RuntimeError("Capability reload result lost its JSON object shape")
        return safe

    def reconnect_mcp_server(self, server_id: str) -> None:
        """Request a fresh physical generation for future safe-point borrows."""

        self._require_open()
        self._mcp_supervisor.reconnect(server_id)

    def inspect_capability_catalog(self) -> KernelCapabilityCatalogInspection:
        """Freeze one UI-safe view without creating another capability owner."""

        self._require_open()
        mcp = self._tools.inspect_mcp_discovery_catalog()
        skills = self._capabilities.freeze_owner_snapshot(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        return KernelCapabilityCatalogInspection(
            mcp_catalog=mcp.catalog_snapshot,
            mcp_configured_servers=mcp.configured_servers,
            mcp_tools=tuple(
                KernelMcpToolCatalogItem(
                    server_id=tool.semantic.server_id,
                    remote_name=tool.semantic.remote_tool_name,
                    provider_name=tool.semantic.provider_tool_name,
                    description=tool.semantic.description,
                    effect=tool.policy.effect_kind.value,
                    available_to_subagents=tool.semantic.subagent_visible,
                    parallel_safe=tool.policy.parallel_safe,
                )
                for tool in mcp.tools
            ),
            skill_catalog=skills.inspection,
            configured_active_skill_names=(
                self._capabilities.configured_active_skill_names
            ),
        )

    @property
    def writer_generation(self) -> int:
        return self._lease.guard.writer_generation

    @property
    def active_skill_names(self) -> frozenset[str]:
        return self._capabilities.configured_active_skill_names

    def current_todo_snapshots(self) -> tuple[FrozenTodoSnapshot, ...]:
        """Read the bounded same-Host TODO inventory for live resync."""

        return self._tools.todo_owner.current_snapshots()

    def _canonical_deadline(self) -> float:
        """Issue a fresh watchdog for one foreground canonical operation."""

        return self._deadlines.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL)

    async def model_call_binding(self) -> ModelCallBinding | None:
        """Read the canonical choice for the next NEW_TURN."""

        self._require_open()
        return await self._io.run(
            self.repository.read_session_model_call_binding,
            self._lease.guard,
            deadline_monotonic=self._canonical_deadline(),
        )

    async def update_model_call_binding(
        self, binding: ModelCallBinding
    ) -> ModelCallBinding:
        """Reconcile and replace only the next-NEW_TURN model choice."""

        self._require_open()
        accepted, _resolved, _changed = (
            self._model_runtime.freeze_resolution_snapshot().reconcile(binding)
        )
        return await self._io.run(
            self.repository.update_session_model_call_binding,
            self._lease.guard,
            binding=accepted,
            deadline_monotonic=self._canonical_deadline(),
        )

    def _model_identity(self, binding: ModelCallBinding) -> str:
        return self._model_runtime.connection(binding).target.model_id

    async def _claim_ingress_hook_attempt(
        self, key: _IngressHookReservationKey
    ) -> tuple[_IngressHookAttempt, bool]:
        async with self._lock:
            self._require_open()
            current = self._ingress_hook_attempts.get(key.command_id)
            if current is not None:
                if current.key != key:
                    raise ConversationKernelConflict(
                        "in-flight prompt command has a different Hook candidate"
                    )
                return current, False
            attempt = _IngressHookAttempt(
                key,
                asyncio.get_running_loop().create_future(),
            )
            self._ingress_hook_attempts[key.command_id] = attempt
            return attempt, True

    async def _settle_ingress_hook_attempt(
        self,
        attempt: _IngressHookAttempt,
        *,
        result: KernelRunResult | KernelCommandOutcome | None = None,
        error: BaseException | None = None,
    ) -> None:
        async with self._lock:
            if self._ingress_hook_attempts.get(attempt.key.command_id) is attempt:
                self._ingress_hook_attempts.pop(attempt.key.command_id, None)
            if attempt.future.done():
                return
            if error is None:
                if result is None:
                    raise RuntimeError("ingress Hook attempt settlement is incomplete")
                attempt.future.set_result(result)
            elif isinstance(error, asyncio.CancelledError):
                attempt.future.cancel()
            else:
                attempt.future.set_exception(error)
                # The owner has already observed the exception.  Mark the shared
                # future as retrieved even when no exact retry joined it.
                attempt.future.exception()

    async def _dispatch_user_prompt_hook(
        self,
        *,
        key: _IngressHookReservationKey,
        identity: PreparedRootTurnIdentity,
        permission: FrozenRunPermissionSnapshot,
        model_call_binding: ModelCallBinding,
    ) -> tuple[str | None, PendingHookContextReservation | None]:
        view = self._hooks.capture_view()
        cwd = self._tools.snapshot_terminal_cwd()
        public_input = UserPromptSubmitInput(
            session_id=self.session_id,
            cwd=str(cwd),
            model=self._model_identity(model_call_binding),
            turn_id=identity.turn_id,
            prompt=key.prompt_utf8.decode("utf-8"),
            permission_mode=external_permission_mode(
                permission.effective_mode.value,
                active_plan_workflow=permission.plan_workflow_id is not None,
            ),
        )
        if key.api_variant is _IngressHookApiVariant.DIRECT:
            causal_ref = DirectPromptRef(
                key.command_id,
                identity.turn_id,
                identity.entry_id,
                identity.context_revision_id,
            )
        else:
            assert key.queue_item_id is not None
            causal_ref = QueuedPromptRef(
                key.command_id,
                key.queue_item_id,
                identity.turn_id,
                identity.entry_id,
                identity.context_revision_id,
            )
        outcome = await self._hooks.dispatch(
            HookDispatchEnvelope(
                view,
                self._hook_root_scope,
                public_input,
                causal_ref,
                self._deadlines.deadline(
                    KernelWatchdogOwner.PROVIDER_DISPATCH_PLANNING
                ),
            ),
            matcher_subject=event_matcher_subject(public_input.event_type),
        )
        if outcome.decision is GateDecision.BLOCK:
            return outcome.reason or "UserPromptSubmit Hook blocked", None
        return None, self._hook_context.prepare_sync(
            scope=self._hook_root_scope,
            causal_ref=causal_ref,
            entries=outcome.context_entries,
        )

    async def register_extension(
        self, request: ExtensionRegistrationRequest
    ) -> ExtensionRegistrationLease:
        self._require_open()
        if request.plane is ExtensionPlane.LIVE:
            generation, revision = self.live_bus.current_cut()
        else:
            generation, revision = self.extensions.current_cut(request.plane)
        return self.extensions.register(
            request,
            registration_cut_generation=generation,
            registration_cut_revision=revision,
        )

    def authenticate_extension_principal(
        self, *, extension_principal_id: str
    ) -> ExtensionPrincipal:
        self._require_open()
        return self.extensions.authenticate_principal(
            extension_principal_id=extension_principal_id,
        )

    async def run_turn(
        self,
        text: str,
        *,
        command_id: str | None = None,
        requested_permission_mode: PermissionMode | None = None,
    ) -> KernelRunResult:
        _validate_prompt(text)
        command = command_id or f"command:{uuid4().hex}"
        requested = requested_permission_mode or self._launch_permission_mode
        identity = build_direct_root_turn_identity(self.session_id, command)
        key = _IngressHookReservationKey(
            _IngressHookApiVariant.DIRECT,
            command,
            text.encode("utf-8"),
            requested_permission_mode,
            None,
        )
        attempt, owner = await self._claim_ingress_hook_attempt(key)
        if not owner:
            joined = await asyncio.shield(attempt.future)
            if not isinstance(joined, KernelRunResult):
                raise RuntimeError("direct prompt joined a queued ingress attempt")
            return joined
        try:
            existing = await self._query_command_row(command)
            if existing is not None:
                raise RuntimeError("command was already accepted; query its outcome")
            async with self._lock:
                self._require_open()
                self._retire_done_active_root_locked()
                if (
                    self._compaction.is_fenced(
                        scope_kind=ModelInputScopeKind.ROOT,
                        scope_subagent_task_id=None,
                    )
                    or self._plan_exit_fence
                    or self._external_new_turn_accepting
                    or self._active_task is not None
                ):
                    raise RuntimeError("a canonical ROOT turn is already running")
            permission = await self._io.run(
                self.repository.prepare_root_permission_snapshot,
                self._lease.guard,
                snapshot_id=identity.permission_snapshot_id,
                requested_mode=requested,
                deadline_monotonic=self._canonical_deadline(),
            )
            model_resolution_snapshot = self._model_runtime.freeze_resolution_snapshot()
            model_call_binding = await self.model_call_binding()
            if model_call_binding is None:
                raise ValueError("session has no model configuration")
            model_call_binding, _resolved, _changed = (
                model_resolution_snapshot.reconcile(model_call_binding)
            )
            block_reason, context_reservation = await self._dispatch_user_prompt_hook(
                key=key,
                identity=identity,
                permission=permission,
                model_call_binding=model_call_binding,
            )
            if block_reason is not None:
                raise PromptBlockedByHook(block_reason)
            async with self._lock:
                self._require_open()
                self._retire_done_active_root_locked()
                if (
                    self._ingress_hook_attempts.get(command) is not attempt
                    or self._compaction.is_fenced(
                        scope_kind=ModelInputScopeKind.ROOT,
                        scope_subagent_task_id=None,
                    )
                    or self._plan_exit_fence
                    or self._external_new_turn_accepting
                    or self._active_task is not None
                ):
                    if context_reservation is not None:
                        context_reservation.retire()
                    raise RuntimeError("prompt Hook admission became stale")
                task = self._install_active_root_task_locked(
                    turn_id=identity.turn_id,
                    command_id=command,
                    name=f"kernel-turn:{command}",
                    run=lambda intent: self._run_root_turn_chain(
                        text,
                        command_id=command,
                        requested_permission_mode=requested,
                        expected_permission_snapshot=permission,
                        hook_context_reservation=context_reservation,
                        cancellation_intent=intent,
                        model_resolution_snapshot=model_resolution_snapshot,
                    ),
                )
            result = await asyncio.shield(task)
        except BaseException as exc:
            await self._settle_ingress_hook_attempt(attempt, error=exc)
            raise
        await self._settle_ingress_hook_attempt(attempt, result=result)
        return result

    async def compact_context(
        self,
        *,
        command_id: str,
        force: bool = False,
        expected_active_turn_id: str | None = None,
    ) -> CompactionOutcome:
        """Request one ROOT compaction without attaching waiter lifetime.

        Active compaction is consumed by the existing ROOT runner at its next
        provider-safe boundary.  Idle compaction is implemented by the same
        owner later in this module; no command creates a durable execution
        attempt or background job.
        """

        if not command_id:
            raise ValueError("compaction command identity is required")

        # Query the stable semantic command before consulting replaceable Host
        # liveness.  An ACK-unknown retry must observe the original target, not
        # silently retarget the same command ID to a newly active/idle turn.
        existing = await self._query_command_row(command_id)
        if existing is not None and str(existing.get("command_kind")) != (
            "COMPACT_CONTEXT"
        ):
            return CompactionOutcome(
                CompactionDisposition.FAILED,
                str(existing.get("target_turn_id") or ""),
                None,
                None,
                "COMMAND_CONFLICT",
            )
        existing_target = (
            None
            if existing is None
            else str(existing.get("target_turn_id") or "") or None
        )
        async with self._lock:
            self._require_open()
            self._retire_done_active_root_locked()
            active_turn_id = self._active_turn_id
            active = self._active_task is not None and active_turn_id is not None
            if expected_active_turn_id is not None and (
                not active or active_turn_id != expected_active_turn_id
            ):
                return CompactionOutcome(
                    CompactionDisposition.FAILED,
                    expected_active_turn_id,
                    None,
                    None,
                    "TARGET_TURN_CHANGED",
                )
        if existing_target is not None:
            target_turn_id = existing_target
        elif active:
            assert active_turn_id is not None
            target_turn_id = active_turn_id
        else:
            target_turn_id = await self._io.run(
                self.repository.read_latest_terminal_scope_turn_id,
                self._lease.guard,
                scope_kind=ConversationScopeKind.ROOT,
                scope_subagent_task_id=None,
                deadline_monotonic=self._canonical_deadline(),
            )
            if target_turn_id is None:
                return CompactionOutcome(
                    CompactionDisposition.NOT_NEEDED,
                    "idle",
                    None,
                    None,
                    "NO_TERMINAL_TURN",
                )

        candidate = build_prepared_manual_compaction_command(
            session_id=self.session_id,
            command_id=command_id,
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
            target_turn_id=target_turn_id,
            expected_active_turn_id=expected_active_turn_id,
            force=force,
        )
        confirmation = await self._settle_manual_compaction_command(candidate)
        if confirmation is not CompactionConfirmationKind.FULL:
            return CompactionOutcome(
                CompactionDisposition.FAILED,
                target_turn_id,
                None,
                None,
                "COMMAND_CONFLICT",
            )
        if existing is not None:
            local = await self._compaction.find_manual(
                command_id=command_id,
                scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
            )
            if local is not None:
                return await asyncio.shield(local)
            # A replacement Host never resumes an old process-local summary.
            # The stable command remains queryable, but execution is not
            # replayed merely because its original waiter/Host disappeared.
            return CompactionOutcome(
                CompactionDisposition.DEFERRED_TO_SAFE_POINT,
                target_turn_id,
                None,
                None,
                "HISTORICAL_COMMAND_ACCEPTED",
            )

        launch_idle = False
        reject_changed_target = False
        # Publication of the process-local request and the active->idle
        # decision share the ROOT-slot lock with task-owned finalization.  A
        # request is therefore either visible to the exact active task before
        # it clears its slot, or is claimed here after that slot is idle; there
        # is no unowned interval between those two states.
        async with self._lock:
            self._require_open()
            self._retire_done_active_root_locked()
            request, future = await self._compaction.request_manual(
                command_id=command_id,
                scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
                expected_turn_id=target_turn_id,
                force=force,
            )
            if self._active_task is None:
                launch_idle = await self._compaction.claim_manual_execution(request)
            elif self._active_turn_id != target_turn_id:
                reject_changed_target = True
        if reject_changed_target:
            await self._compaction.settle_manual(
                request,
                CompactionOutcome(
                    CompactionDisposition.FAILED,
                    target_turn_id,
                    None,
                    None,
                    "TARGET_TURN_CHANGED",
                ),
            )
        elif launch_idle:
            self._compaction.start_owned(
                self._execute_idle_manual_compaction(request),
                name=f"kernel-idle-compaction:{command_id}",
            )
        return await asyncio.shield(future)

    async def _execute_idle_manual_compaction(
        self, request: ManualCompactionRequest
    ) -> None:
        target_turn_id = request.expected_turn_id
        if target_turn_id is None:
            outcome = CompactionOutcome(
                CompactionDisposition.FAILED,
                "idle",
                None,
                None,
                "TARGET_TURN_CHANGED",
            )
        else:
            try:
                outcome = await self._runner.compact_idle_turn(
                    turn_id=target_turn_id,
                    command_id=request.command_id,
                    force=request.force,
                )
            except StaleHostWriter:
                outcome = CompactionOutcome(
                    CompactionDisposition.FAILED,
                    target_turn_id,
                    None,
                    None,
                    "WRITER_REPLACED",
                )
            except asyncio.CancelledError:
                outcome = CompactionOutcome(
                    CompactionDisposition.FAILED,
                    target_turn_id,
                    None,
                    None,
                    "HOST_CLOSING" if self._closing else "COMPACTION_CANCELLED",
                )
                await self._compaction.settle_manual(request, outcome)
                raise
            except BaseException:
                outcome = CompactionOutcome(
                    CompactionDisposition.FAILED,
                    target_turn_id,
                    None,
                    None,
                    "COMPACTION_FAILED",
                )
        await self._compaction.settle_manual(request, outcome)

    async def _settle_manual_compaction_command(
        self, candidate: PreparedManualCompactionCommand
    ) -> CompactionConfirmationKind:
        existing = self._manual_compaction_command_attempts.get(candidate.command_id)
        if existing is not None:
            semantic_digest, task = existing
            if semantic_digest != candidate.semantic_digest:
                return CompactionConfirmationKind.CONFLICT
        else:
            task = self._compaction.start_settlement(
                self._settle_manual_compaction_command_worker(candidate),
                name=f"kernel-compaction-command:{candidate.command_id}",
            )
            self._manual_compaction_command_attempts[candidate.command_id] = (
                candidate.semantic_digest,
                task,
            )

            def retire(completed: asyncio.Task[CompactionConfirmationKind]) -> None:
                current = self._manual_compaction_command_attempts.get(
                    candidate.command_id
                )
                if current is not None and current[1] is completed:
                    self._manual_compaction_command_attempts.pop(
                        candidate.command_id, None
                    )

            task.add_done_callback(retire)
        return await asyncio.shield(task)

    async def _settle_manual_compaction_command_worker(
        self, candidate: PreparedManualCompactionCommand
    ) -> CompactionConfirmationKind:
        delay = 0.05
        while True:
            try:
                confirmation = await self._io.run(
                    self.repository.confirm_manual_compaction_command,
                    candidate=candidate,
                    deadline_monotonic=self._canonical_deadline(),
                )
            except StaleHostWriter:
                raise
            except BaseException:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 0.5)
                continue
            if confirmation is CompactionConfirmationKind.FULL:
                return confirmation
            if confirmation is CompactionConfirmationKind.CONFLICT:
                return confirmation
            try:
                written = await self._io.run(
                    self.repository.accept_manual_compaction_command,
                    self._lease.guard,
                    candidate=candidate,
                    deadline_monotonic=self._canonical_deadline(),
                )
                if written is CompactionConfirmationKind.FULL:
                    return written
                if written is CompactionConfirmationKind.CONFLICT:
                    return written
            except StaleHostWriter:
                raise
            except BaseException:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 0.5)
                continue
            await asyncio.sleep(0)

    def current_compaction_projection(
        self,
    ) -> tuple[bool, str | None, str | None, str | None]:
        """Return the bounded same-Host manual/automatic compaction view."""

        return self._compaction.current_projection()

    async def _run_root_turn_chain(
        self,
        text: str,
        *,
        command_id: str,
        requested_permission_mode: PermissionMode,
        expected_permission_snapshot: FrozenRunPermissionSnapshot,
        hook_context_reservation: PendingHookContextReservation | None,
        cancellation_intent: ActiveTurnCancellationIntent,
        model_resolution_snapshot: FrozenModelResolutionSnapshot,
    ) -> KernelRunResult:
        result = await self._runner.run_turn(
            text,
            command_id=command_id,
            requested_permission_mode=requested_permission_mode,
            expected_permission_snapshot=expected_permission_snapshot,
            hook_context_reservation=hook_context_reservation,
            cancellation_intent=cancellation_intent,
            model_resolution_snapshot=model_resolution_snapshot,
        )
        return await self._finish_root_chain(result, cancellation_intent)

    async def _run_accepted_root_chain(
        self,
        turn_id: str,
        cancellation_intent: ActiveTurnCancellationIntent,
    ) -> KernelRunResult:
        """Run one accepted ROOT turn and every automatic successor."""

        result = await self._runner.run_accepted_turn(
            turn_id, cancellation_intent=cancellation_intent
        )
        return await self._finish_root_chain(result, cancellation_intent)

    async def _finish_root_chain(
        self,
        result: KernelRunResult,
        cancellation_intent: ActiveTurnCancellationIntent,
    ) -> KernelRunResult:
        total_model_calls = result.model_call_count
        total_tool_calls = result.tool_call_count
        while result.continuation_turn_id is not None:
            continuation_turn_id = result.continuation_turn_id
            async with self._lock:
                if self._active_task is not asyncio.current_task():
                    raise RuntimeError("ROOT continuation lost its Host task owner")
                pending = self._pending_root_successor
                if (
                    pending is None
                    or pending.owner_task is not asyncio.current_task()
                    or pending.origin_turn_id != self._active_turn_id
                    or pending.successor_turn_id != continuation_turn_id
                    or self._active_root_phase
                    is not RootChainPhase.SUCCESSOR_COMMITTED_PENDING_HANDOFF
                ):
                    raise RuntimeError(
                        "ROOT continuation lost its closed pending handoff"
                    )
                if (
                    self._closing
                    or self._plan_exit_fence
                    or asyncio.current_task().cancelling() != 0
                ):
                    raise asyncio.CancelledError
                self._tools.todo_owner.bind_continuation(
                    scope_kind=ModelInputScopeKind.ROOT,
                    scope_subagent_task_id=None,
                    turn_id=continuation_turn_id,
                )
                if self._control_completion_sealed_turn_id == pending.origin_turn_id:
                    self._control_completion_sealed_turn_id = None
                self._active_turn_id = continuation_turn_id
                cancellation_intent = ActiveTurnCancellationIntent(
                    continuation_turn_id, ModelInputScopeKind.ROOT, None
                )
                self._active_cancellation_intent = cancellation_intent
                self._pending_root_successor = None
                self._active_root_phase = RootChainPhase.SUCCESSOR_RUNNING
            result = await self._runner.run_accepted_turn(
                continuation_turn_id,
                cancellation_intent=cancellation_intent,
            )
            total_model_calls += result.model_call_count
            total_tool_calls += result.tool_call_count
        return KernelRunResult(
            turn_id=result.turn_id,
            final_entry_id=result.final_entry_id,
            final_text=result.final_text,
            model_call_count=total_model_calls,
            tool_call_count=total_tool_calls,
            pending_plan_interaction_id=result.pending_plan_interaction_id,
        )

    def _install_active_root_task_locked(
        self,
        *,
        turn_id: str,
        command_id: str | None,
        name: str,
        run: Callable[[ActiveTurnCancellationIntent], Awaitable[KernelRunResult]],
    ) -> asyncio.Task[KernelRunResult]:
        """Install the sole Host-owned ROOT task while ``self._lock`` is held."""

        self._retire_done_active_root_locked()
        if self._active_task is not None:
            raise RuntimeError("a canonical ROOT turn is already running")
        intent = ActiveTurnCancellationIntent(turn_id, ModelInputScopeKind.ROOT, None)
        task = asyncio.create_task(
            self._run_owned_root_task(run, intent),
            name=name,
        )
        self._active_task = task
        self._active_turn_id = turn_id
        self._active_cancellation_intent = intent
        self._active_command_id = command_id
        self._active_root_phase = RootChainPhase.ORIGIN_RUNNING
        self._pending_root_successor = None
        task.add_done_callback(self._observe_active_root_task_done)
        return task

    @staticmethod
    def _observe_active_root_task_done(task: asyncio.Task[KernelRunResult]) -> None:
        """Consume a detached ROOT task exception after task-owned settlement."""

        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logging.getLogger(__name__).error(
                "ROOT execution failed (%s)",
                task.get_name(),
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _run_owned_root_task(
        self,
        run: Callable[[ActiveTurnCancellationIntent], Awaitable[KernelRunResult]],
        intent: ActiveTurnCancellationIntent,
    ) -> KernelRunResult:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("ROOT task has no asyncio owner")
        result: KernelRunResult | None = None
        try:
            result = await run(intent)
            return result
        finally:
            await self._settle_pending_root_successor(task)
            await self._settle_active_root_task(task)

    async def _settle_pending_root_successor(self, task: asyncio.Task[object]) -> None:
        async with self._lock:
            pending = self._pending_root_successor
            if pending is None or pending.owner_task is not task:
                return
            self._pending_root_successor = None
            intent = self._active_cancellation_intent
            cause = None if intent is None else intent.cause
        # The origin chain owns this already-committed successor until the
        # exact turn is terminal.  Caller cancellation cannot detach it, and
        # no replacement process-local task is installed.
        settlement = asyncio.create_task(
            self._terminalize_pending_root_successor(
                pending,
                reason=(
                    "USER_STOPPED"
                    if cause is ForegroundCancellationCause.USER_REQUEST
                    else "SESSION_CLOSED"
                    if cause is ForegroundCancellationCause.HOST_SESSION_CLOSE
                    else "PLAN_CONTINUATION_NOT_BOUND"
                ),
            ),
            name=f"kernel-root-successor-terminalization:{pending.successor_turn_id}",
        )
        while True:
            try:
                await asyncio.shield(settlement)
                break
            except asyncio.CancelledError:
                if settlement.done():
                    settlement.result()
                    break
                # A repeated stop/close cancellation only detaches its waiter;
                # it cannot cancel or replace this exact successor settlement.
                continue

    async def _settle_active_root_task(self, task: asyncio.Task[object]) -> None:
        manual: ManualCompactionRequest | None = None
        async with self._lock:
            if self._active_task is task:
                active_turn_id = self._active_turn_id
                if active_turn_id is not None and not self._closing:
                    manual = await self._compaction.take_manual(
                        scope_kind=ModelInputScopeKind.ROOT,
                        scope_subagent_task_id=None,
                        turn_id=active_turn_id,
                    )
                if manual is None and active_turn_id is not None:
                    self._tools.todo_owner.mark_root_idle(exact_turn_id=active_turn_id)
                if manual is None:
                    if active_turn_id is not None:
                        self._mark_feedback_target_ended_locked(active_turn_id)
                    self._clear_active_root_locked()
        if manual is None:
            return

        # The exact ROOT task remains the visible slot owner until this
        # already-started request reaches a terminal process-local outcome.
        # Repeated waiter/close cancellation can detach this await, but cannot
        # strand the request or clear the slot ahead of settlement.
        settlement = asyncio.create_task(
            self._execute_idle_manual_compaction(manual),
            name=f"kernel-terminal-manual-compaction:{manual.command_id}",
        )
        try:
            while True:
                try:
                    await asyncio.shield(settlement)
                    break
                except asyncio.CancelledError:
                    if settlement.done():
                        settlement.result()
                        break
                    continue
        finally:
            async with self._lock:
                if self._active_task is task:
                    active_turn_id = self._active_turn_id
                    if active_turn_id is not None:
                        self._tools.todo_owner.mark_root_idle(
                            exact_turn_id=active_turn_id
                        )
                        self._mark_feedback_target_ended_locked(active_turn_id)
                    self._clear_active_root_locked()

    async def _finalize_todo_run_activation(
        self,
        prepared: PreparedTodoRootRunActivation | PreparedTodoChildRunActivation,
        accepted: object,
    ) -> None:
        async with self._lock:
            closed = self._finalize_todo_run_activation_locked(prepared, accepted)
        self._tools.offer_todo_close(closed)

    def _finalize_todo_run_activation_locked(
        self,
        prepared: PreparedTodoRootRunActivation | PreparedTodoChildRunActivation,
        accepted: object,
    ) -> FrozenTodoCloseProjection | None:
        """Apply the common admission finalizer while the Host lock is held."""

        if (
            accepted.turn_id != prepared.exact_turn_id
            or accepted.entry_id != prepared.exact_initial_entry_id
        ):
            raise RuntimeError("TODO activation does not exact-join admission")
        if isinstance(prepared, PreparedTodoRootRunActivation):
            return self._tools.todo_owner.activate_root_run(
                prepared,
                allow_closing_predecessor=self._closing,
            )
        self._tools.todo_owner.activate_child_run(prepared)
        return None

    def _retire_done_active_root_locked(self) -> None:
        task = self._active_task
        if task is not None and task.done():
            self._clear_active_root_locked()

    def _clear_active_root_locked(self) -> None:
        if self._pending_root_successor is not None:
            raise RuntimeError("ROOT slot cleared with an unsettled successor handoff")
        active_turn_id = self._active_turn_id
        if active_turn_id is not None:
            self._mark_feedback_target_ended_locked(active_turn_id)
        if getattr(self, "_control_completion_sealed_turn_id", None) == active_turn_id:
            self._control_completion_sealed_turn_id = None
        self._active_task = None
        self._active_turn_id = None
        self._active_cancellation_intent = None
        self._active_command_id = None
        self._active_root_phase = None
        self._queue_wake.set()
        self._monitor_wake.set()

    def _admit_plan_resolution_write_locked(
        self, *, creates_turn: bool
    ) -> tuple[ModelInputScopeKind, str | None]:
        """Linearize every new Plan-resolution write against force exit."""

        self._require_open()
        if self._plan_exit_fence:
            raise ConversationKernelConflict(
                "Plan resolution conflicts with force exit"
            )
        if self._compaction.is_fenced(
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        ):
            raise ConversationKernelConflict(
                "Plan resolution conflicts with context compaction"
            )
        if creates_turn:
            self._retire_done_active_root_locked()
            if self._external_new_turn_accepting or self._active_task is not None:
                raise ConversationKernelConflict(
                    "a canonical ROOT turn is already running"
                )
        reservation = self._reserve_compaction_write_locked(
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        if creates_turn:
            self._external_new_turn_accepting = True
            self._external_new_turn_settled.clear()
        return reservation

    async def _accept_automatic_plan_continuation(
        self,
        candidate: PreparedPlanToolBatch,
        deadline_monotonic: float,
    ) -> AcceptedPlanToolBatch:
        if candidate.continuation_turn_id is None:
            raise ValueError("automatic Plan continuation candidate has no turn")
        if candidate.continuation_entry_id is None:
            raise ValueError("automatic Plan continuation candidate has no entry")
        origin_task = asyncio.current_task()
        if origin_task is None:
            raise RuntimeError("Plan continuation has no Host-owned origin task")

        attempt_id = f"plan-continuation:{candidate.candidate_fingerprint}"

        async def accept() -> AcceptedPlanToolBatch:
            try:
                result = await self._io.run(
                    self.repository.accept_plan_tool_batch,
                    self._lease.guard,
                    candidate=candidate,
                    deadline_monotonic=deadline_monotonic,
                )
            except Exception:
                winner = await self._io.run(
                    self.repository.confirm_plan_tool_batch_winner,
                    candidate=candidate,
                    deadline_monotonic=self._canonical_deadline(),
                )
                if winner is None:
                    raise
                result = winner
            try:
                inspection = await self._inspect_plan_continuation(
                    turn_id=candidate.continuation_turn_id,
                    entry_id=candidate.continuation_entry_id,
                    workflow_id=candidate.workflow_id,
                    interaction_id=None,
                    handoff_kind=PlanHandoffKind.ENTERED_PLAN,
                )
                disposition = self.repository.classify_plan_continuation(
                    inspection, self._lease.guard
                )
                if disposition is PlanContinuationDisposition.HISTORICAL_TERMINAL:
                    return replace(
                        result,
                        continuation_turn_id=None,
                        continuation_entry_id=None,
                    )
                async with self._lock:
                    current_writer = (
                        disposition
                        is PlanContinuationDisposition.RUNNING_CURRENT_WRITER
                    )
                    compatible_slot = (
                        self._active_task is origin_task
                        and self._active_turn_id == candidate.origin_turn_id
                    )
                    origin_runner_live = (
                        not origin_task.done() and origin_task.cancelling() == 0
                    )
                    if (
                        not self._closing
                        and not self._plan_exit_fence
                        and current_writer
                        and compatible_slot
                        and origin_runner_live
                    ):
                        if self._pending_root_successor is not None:
                            raise ConversationKernelConflict(
                                "ROOT chain already owns a pending successor"
                            )
                        # The canonical successor is FULL, but the outer ROOT
                        # chain still executes the origin runner.  Preserve the
                        # origin cancellation intent until _finish_root_chain()
                        # atomically promotes this closed handoff.
                        self._pending_root_successor = _PendingRootSuccessorHandoff(
                            owner_task=origin_task,
                            attempt_id=attempt_id,
                            origin_turn_id=candidate.origin_turn_id,
                            successor_turn_id=candidate.continuation_turn_id,
                            successor_entry_id=candidate.continuation_entry_id,
                            workflow_id=candidate.workflow_id,
                            interaction_id=None,
                            handoff_kind=PlanHandoffKind.ENTERED_PLAN,
                        )
                        self._active_root_phase = (
                            RootChainPhase.SUCCESSOR_COMMITTED_PENDING_HANDOFF
                        )
                        return result
            except BaseException:
                # The canonical continuation may already be FULL.  The same
                # process-local attempt must own its terminalization until the
                # exact turn is terminal or belongs to a replacement writer.
                pass
            await self._terminalize_unbound_plan_successor(
                attempt_id=attempt_id,
                turn_id=candidate.continuation_turn_id,
                entry_id=candidate.continuation_entry_id,
                workflow_id=candidate.workflow_id,
                interaction_id=None,
                handoff_kind=PlanHandoffKind.ENTERED_PLAN,
            )
            return replace(
                result,
                continuation_turn_id=None,
                continuation_entry_id=None,
            )

        async with self._lock:

            def require_new_attempt() -> None:
                if (
                    self._closing
                    or self._plan_exit_fence
                    or self._compaction.is_fenced(
                        scope_kind=ModelInputScopeKind.ROOT,
                        scope_subagent_task_id=None,
                    )
                    or self._active_task is not origin_task
                    or self._active_turn_id != candidate.origin_turn_id
                    or origin_task.done()
                    or origin_task.cancelling() != 0
                ):
                    raise ConversationKernelConflict(
                        "automatic Plan continuation admission is stale"
                    )

            attempt = self._plan_continuations.start(
                attempt_id=attempt_id,
                turn_id=candidate.continuation_turn_id,
                semantic_candidate_fingerprint=candidate.candidate_fingerprint,
                run=accept,
                before_start=require_new_attempt,
            )
        result = await asyncio.shield(attempt.task)
        if not isinstance(result, AcceptedPlanToolBatch):
            raise RuntimeError("Plan continuation owner returned an invalid result")
        return result

    async def submit_prompt(
        self,
        *,
        command_id: str,
        text: str,
        delivery_mode: PromptDeliveryMode = PromptDeliveryMode.NEW_TURN,
        target_turn_id: str | None = None,
        requested_permission_mode: PermissionMode | None = None,
    ) -> KernelCommandOutcome:
        if not command_id or (delivery_mode is PromptDeliveryMode.NEW_TURN) != (
            target_turn_id is None
        ):
            return await self._submit_prompt_owner(
                command_id=command_id,
                text=text,
                delivery_mode=delivery_mode,
                target_turn_id=target_turn_id,
                requested_permission_mode=requested_permission_mode,
            )
        try:
            _validate_prompt(text)
        except ValueError:
            return await self._submit_prompt_owner(
                command_id=command_id,
                text=text,
                delivery_mode=delivery_mode,
                target_turn_id=target_turn_id,
                requested_permission_mode=requested_permission_mode,
            )
        if delivery_mode is PromptDeliveryMode.STEER_ACTIVE_TURN:
            return await self._submit_prompt_owner(
                command_id=command_id,
                text=text,
                delivery_mode=delivery_mode,
                target_turn_id=target_turn_id,
                requested_permission_mode=requested_permission_mode,
            )
        queue_item_id = _stable_id("queue-item", self.session_id, command_id)
        key = _IngressHookReservationKey(
            _IngressHookApiVariant.QUEUED,
            command_id,
            text.encode("utf-8"),
            requested_permission_mode,
            queue_item_id,
        )
        try:
            attempt, owner = await self._claim_ingress_hook_attempt(key)
        except ConversationKernelConflict:
            return KernelCommandOutcome(
                command_id,
                "REJECTED",
                queue_item_id,
                "COMMAND_CONFLICT",
                "The command identity names a different in-flight prompt.",
            )
        if not owner:
            joined = await asyncio.shield(attempt.future)
            if not isinstance(joined, KernelCommandOutcome):
                raise RuntimeError("queued prompt joined a direct ingress attempt")
            return joined
        try:
            result = await self._submit_prompt_owner(
                command_id=command_id,
                text=text,
                delivery_mode=delivery_mode,
                target_turn_id=target_turn_id,
                requested_permission_mode=requested_permission_mode,
                hook_attempt=attempt,
            )
        except BaseException as exc:
            await self._settle_ingress_hook_attempt(attempt, error=exc)
            raise
        await self._settle_ingress_hook_attempt(attempt, result=result)
        return result

    async def _submit_prompt_owner(
        self,
        *,
        command_id: str,
        text: str,
        delivery_mode: PromptDeliveryMode = PromptDeliveryMode.NEW_TURN,
        target_turn_id: str | None = None,
        requested_permission_mode: PermissionMode | None = None,
        hook_attempt: _IngressHookAttempt | None = None,
    ) -> KernelCommandOutcome:
        if not command_id:
            return KernelCommandOutcome(
                command_id, "REJECTED", "", "INVALID_REQUEST", "Command is invalid."
            )
        try:
            _validate_prompt(text)
        except ValueError:
            return KernelCommandOutcome(
                command_id, "REJECTED", "", "INVALID_PROMPT", "Prompt is invalid."
            )
        self._require_open()
        if (delivery_mode is PromptDeliveryMode.NEW_TURN) != (target_turn_id is None):
            return KernelCommandOutcome(
                command_id,
                "REJECTED",
                "",
                "INVALID_STEER_TARGET",
                "Prompt delivery target is invalid.",
            )
        queue_item_id = _stable_id("queue-item", self.session_id, command_id)
        permission_snapshot_id = (
            None
            if delivery_mode is PromptDeliveryMode.STEER_ACTIVE_TURN
            else _stable_id("permission-snapshot", self.session_id, queue_item_id)
        )
        effective_requested_permission = (
            None
            if delivery_mode is PromptDeliveryMode.STEER_ACTIVE_TURN
            else requested_permission_mode or self._launch_permission_mode
        )
        content_utf8 = text.encode("utf-8")
        ingress = build_prompt_ingress_command(
            session_id=self.session_id,
            command_id=command_id,
            queue_item_id=queue_item_id,
            client_submission_id=command_id,
            delivery_mode=delivery_mode,
            target_turn_id=target_turn_id,
            permission_snapshot_id=permission_snapshot_id,
            requested_permission_mode=effective_requested_permission,
            content_utf8=content_utf8,
        )
        confirmation = await self._io.run(
            self.repository.confirm_prompt_ingress,
            candidate=ingress,
            deadline_monotonic=self._canonical_deadline(),
        )
        if confirmation.kind is PromptIngressConfirmationKind.CONFLICT:
            return KernelCommandOutcome(
                command_id,
                "REJECTED",
                queue_item_id,
                "COMMAND_CONFLICT",
                "The command identity names a different prompt.",
            )
        if confirmation.kind is PromptIngressConfirmationKind.FULL_COMPATIBLE:
            # The row is the level-triggered delivery truth.  A prior caller
            # may have lost the COMMIT acknowledgement before it could wake
            # the process-local queue loop, so every compatible retry must
            # re-assert the wake hint.
            self._queue_wake.set()
            if delivery_mode is PromptDeliveryMode.STEER_ACTIVE_TURN:
                await self._subagents.notify_root_input_activity()
            existing = await self.query_command(command_id)
            if existing is None:
                raise RuntimeError("compatible prompt command has no canonical outcome")
            return existing
        model_resolution_snapshot: FrozenModelResolutionSnapshot | None = None
        model_call_binding: ModelCallBinding | None = None
        if delivery_mode is PromptDeliveryMode.NEW_TURN:
            model_call_binding = await self.model_call_binding()
            if model_call_binding is None:
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    queue_item_id,
                    "MODEL_CONFIGURATION_REQUIRED",
                    "Select a model configuration before sending.",
                )
            try:
                model_resolution_snapshot = (
                    self._model_runtime.freeze_resolution_snapshot()
                )
                model_call_binding, _resolved, _changed = (
                    model_resolution_snapshot.reconcile(model_call_binding)
                )
            except (KeyError, ValueError, ModelRuntimeUnavailable) as exc:
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    queue_item_id,
                    "MODEL_CONFIGURATION_UNAVAILABLE",
                    str(exc),
                )
        permission_projection: FrozenRunPermissionSnapshot | None = None
        hook_context_reservation: PendingHookContextReservation | None = None
        if delivery_mode is PromptDeliveryMode.NEW_TURN:
            if hook_attempt is None:
                raise RuntimeError("queued new turn lacks its Hook reservation owner")
            identity = build_queued_root_turn_identity(self.session_id, queue_item_id)
            if permission_snapshot_id != identity.permission_snapshot_id:
                raise RuntimeError("queued prompt permission identity drifted")
            assert effective_requested_permission is not None
            permission_projection = await self._io.run(
                self.repository.prepare_root_permission_snapshot,
                self._lease.guard,
                snapshot_id=identity.permission_snapshot_id,
                requested_mode=effective_requested_permission,
                deadline_monotonic=self._canonical_deadline(),
            )
            (
                block_reason,
                hook_context_reservation,
            ) = await self._dispatch_user_prompt_hook(
                key=hook_attempt.key,
                identity=identity,
                permission=permission_projection,
                model_call_binding=model_call_binding,
            )
            if block_reason is not None:
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    queue_item_id,
                    "HOOK_BLOCKED",
                    block_reason,
                )
        async with self._lock:
            if (
                self._closing
                or self._plan_exit_fence
                or (
                    hook_attempt is not None
                    and self._ingress_hook_attempts.get(command_id) is not hook_attempt
                )
            ):
                if hook_context_reservation is not None:
                    hook_context_reservation.retire()
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    "",
                    "PLAN_TRANSITION_BUSY",
                    "A Plan force-exit transition is in progress.",
                )
            try:
                reservation = self._reserve_compaction_write_locked(
                    scope_kind=ModelInputScopeKind.ROOT,
                    scope_subagent_task_id=None,
                )
            except RuntimeError:
                if hook_context_reservation is not None:
                    hook_context_reservation.retire()
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    target_turn_id or "",
                    "COMPACTION_IN_PROGRESS",
                    "Context compaction is in progress for the ROOT scope.",
                )
        try:
            try:
                return await self._submit_prompt_reserved(
                    command_id=command_id,
                    queue_item_id=queue_item_id,
                    delivery_mode=delivery_mode,
                    target_turn_id=target_turn_id,
                    permission_snapshot_id=permission_snapshot_id,
                    effective_requested_permission=effective_requested_permission,
                    content_utf8=content_utf8,
                    ingress=ingress,
                    model_resolution_snapshot=model_resolution_snapshot,
                    expected_permission_snapshot=permission_projection,
                    hook_context_reservation=hook_context_reservation,
                )
            except BaseException:
                if hook_context_reservation is not None:
                    hook_context_reservation.retire()
                raise
        finally:
            await self._release_compaction_write_reservation(reservation)

    async def _submit_prompt_reserved(
        self,
        *,
        command_id: str,
        queue_item_id: str,
        delivery_mode: PromptDeliveryMode,
        target_turn_id: str | None,
        permission_snapshot_id: str | None,
        effective_requested_permission: PermissionMode | None,
        content_utf8: bytes,
        ingress: PreparedPromptIngressCommand,
        model_resolution_snapshot: FrozenModelResolutionSnapshot | None,
        expected_permission_snapshot: FrozenRunPermissionSnapshot | None,
        hook_context_reservation: PendingHookContextReservation | None,
    ) -> KernelCommandOutcome:
        content = await self._io.run(
            self._content_publisher.materialize,
            session_id=self.session_id,
            content=content_utf8,
            media_type="text/plain",
            codec="utf-8",
            deadline_monotonic=self._canonical_deadline(),
        )
        try:
            accepted = await self._io.run(
                self.repository.enqueue_prompt,
                self._lease.guard,
                candidate=ingress,
                model_resolution_snapshot=model_resolution_snapshot,
                content=content,
                occurred_at=datetime.now().astimezone(),
                actor_id=self.host_session_id,
                deadline_monotonic=self._canonical_deadline(),
                _expected_permission_snapshot=expected_permission_snapshot,
            )
        except PromptIngressRejected as exc:
            if hook_context_reservation is not None:
                hook_context_reservation.retire()
            if exc.reason is PromptIngressWriteRejection.COMMAND_CONFLICT:
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    queue_item_id,
                    "COMMAND_CONFLICT",
                    "The command identity names a different prompt.",
                )
            if exc.reason is PromptIngressWriteRejection.CAPACITY_EXHAUSTED:
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    queue_item_id,
                    "PROMPT_CAPACITY_EXHAUSTED",
                    "The session prompt queue is full.",
                )
            if exc.reason is (PromptIngressWriteRejection.INGRESS_PRECONDITION_CHANGED):
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    queue_item_id,
                    "INGRESS_PRECONDITION_CHANGED",
                    "The Plan or permission cut changed while the Hook ran.",
                )
            if exc.reason is PromptIngressWriteRejection.MODEL_CONFIGURATION_REQUIRED:
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    queue_item_id,
                    "MODEL_CONFIGURATION_REQUIRED",
                    "Select a model configuration before sending.",
                )
            if (
                exc.reason
                is PromptIngressWriteRejection.MODEL_CONFIGURATION_UNAVAILABLE
            ):
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    queue_item_id,
                    "MODEL_CONFIGURATION_UNAVAILABLE",
                    "The selected model configuration is no longer executable.",
                )
            if exc.reason is (
                PromptIngressWriteRejection.TARGET_STALE_OR_NON_STEERABLE
            ):
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    target_turn_id or "",
                    "STEER_TARGET_STALE",
                    "The target turn no longer accepts steering.",
                )
            raise
        except Exception:
            confirmation = await self._io.run(
                self.repository.confirm_prompt_ingress,
                candidate=ingress,
                deadline_monotonic=self._canonical_deadline(),
            )
            if confirmation.kind is PromptIngressConfirmationKind.FULL_COMPATIBLE:
                if hook_context_reservation is not None:
                    hook_context_reservation.commit_prompt_bound(queue_item_id)
                self._queue_wake.set()
                if delivery_mode is PromptDeliveryMode.STEER_ACTIVE_TURN:
                    await self._subagents.notify_root_input_activity()
                existing = await self.query_command(command_id)
                if existing is None:
                    raise RuntimeError(
                        "compatible prompt command has no canonical outcome"
                    )
                return existing
            if confirmation.kind is PromptIngressConfirmationKind.CONFLICT:
                if hook_context_reservation is not None:
                    hook_context_reservation.retire()
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    queue_item_id,
                    "COMMAND_CONFLICT",
                    "The command identity names a different prompt.",
                )
            raise
        if hook_context_reservation is not None:
            hook_context_reservation.commit_prompt_bound(queue_item_id)
        self._queue_wake.set()
        if delivery_mode is PromptDeliveryMode.STEER_ACTIVE_TURN:
            await self._subagents.notify_root_input_activity()
        return KernelCommandOutcome(
            command_id,
            "PENDING",
            queue_item_id,
            (
                "PROMPT_QUEUED_REASONING_UPDATED"
                if accepted.reasoning_preference_reset
                else "PROMPT_QUEUED"
            ),
            (
                "Reasoning options changed; the new target default was selected."
                if accepted.reasoning_preference_reset
                else f"Prompt accepted at queue sequence {accepted.queue_sequence}."
            ),
            prompt_delivery=KernelPromptDelivery(
                queue_item_id=queue_item_id,
                queue_status="PENDING",
                consumed_entry_id=None,
                delivery_mode=delivery_mode.value,
            ),
        )

    async def steer_active_turn(
        self, *, command_id: str, text: str, target_turn_id: str
    ) -> KernelCommandOutcome:
        # The stable command is queried before any process-local liveness
        # shortcut.  This lets a retry recover a committed queue winner after
        # its ACK was lost even if the target turn has since become terminal.
        # A genuinely new write is still exact-target validated by the
        # repository in the Host-writer transaction.
        return await self.submit_prompt(
            command_id=command_id,
            text=text,
            delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
            target_turn_id=target_turn_id,
        )

    async def enter_plan(
        self,
        *,
        command_id: str,
        entry_reason: str,
        resume_permission_mode: PermissionMode,
    ) -> KernelCommandOutcome:
        """Install a user-origin Plan workflow while the ROOT slot is idle."""

        workflow_id = _stable_id("plan-workflow", self.session_id, command_id)
        semantic_digest = canonical_digest(
            "pulsara:user-enter-plan:v1",
            {
                "workflow_id": workflow_id,
                "entry_reason": entry_reason,
                "resume_permission_mode": resume_permission_mode.value,
            },
        )
        existing = await self._query_command_row(command_id)
        if existing is not None:
            if (
                str(existing.get("command_kind")) != "ENTER_PLAN"
                or str(existing.get("semantic_digest")) != semantic_digest
                or str(existing.get("target_plan_workflow_id")) != workflow_id
            ):
                raise ConversationKernelConflict("Plan enter command conflicts")
            outcome = await self.query_command(command_id)
            assert outcome is not None
            return outcome
        async with self._lock:
            self._require_open()
            self._retire_done_active_root_locked()
            active = self._active_task
            if (
                self._plan_exit_fence
                or self._compaction.is_fenced(
                    scope_kind=ModelInputScopeKind.ROOT,
                    scope_subagent_task_id=None,
                )
                or self._external_new_turn_accepting
                or active is not None
            ):
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    workflow_id,
                    "ROOT_SLOT_UNAVAILABLE",
                    "Plan mode can only be entered while the ROOT slot is idle.",
                )
            self._external_new_turn_accepting = True
            self._external_new_turn_settled.clear()
        try:
            accepted = await self._io.run(
                self.repository.enter_plan_by_user,
                self._lease.guard,
                command_id=command_id,
                workflow_id=workflow_id,
                entry_reason=entry_reason,
                resume_permission_mode=resume_permission_mode,
                occurred_at=datetime.now().astimezone(),
                actor_id=self.host_session_id,
                deadline_monotonic=self._canonical_deadline(),
            )
            return _plan_workflow_command_outcome(accepted)
        finally:
            await self._release_plan_continuation_reservation()

    async def cancel_plan(
        self,
        *,
        command_id: str,
        workflow_id: str,
        expected_workflow_revision: int,
    ) -> KernelCommandOutcome:
        return await self._exit_plan(
            command_id=command_id,
            command_kind="CANCEL_PLAN",
            workflow_id=workflow_id,
            expected_workflow_revision=expected_workflow_revision,
            force=False,
        )

    async def force_exit_plan(
        self,
        *,
        command_id: str,
        workflow_id: str,
        expected_workflow_revision: int,
    ) -> KernelCommandOutcome:
        return await self._exit_plan(
            command_id=command_id,
            command_kind="FORCE_EXIT_PLAN",
            workflow_id=workflow_id,
            expected_workflow_revision=expected_workflow_revision,
            force=True,
        )

    async def _exit_plan(
        self,
        *,
        command_id: str,
        command_kind: str,
        workflow_id: str,
        expected_workflow_revision: int,
        force: bool,
    ) -> KernelCommandOutcome:
        semantic_digest = plan_exit_semantic_fingerprint(
            command_kind=command_kind,
            workflow_id=workflow_id,
            expected_workflow_revision=expected_workflow_revision,
        )
        existing = await self._query_command_row(command_id)
        if existing is not None:
            if (
                str(existing.get("command_kind")) != command_kind
                or str(existing.get("semantic_digest")) != semantic_digest
                or str(existing.get("target_plan_workflow_id")) != workflow_id
            ):
                raise ConversationKernelConflict("Plan exit command conflicts")
            outcome = await self.query_command(command_id)
            assert outcome is not None
            return outcome
        async with self._lock:
            self._require_open()
            self._retire_done_active_root_locked()
            if (
                self._plan_exit_fence
                or self._compaction.is_fenced(
                    scope_kind=ModelInputScopeKind.ROOT,
                    scope_subagent_task_id=None,
                )
                or self._external_new_turn_accepting
            ):
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    workflow_id,
                    "PLAN_TRANSITION_BUSY",
                    "Another ROOT admission or Plan transition is in progress.",
                )
            active = self._active_task
            active_turn_id = self._active_turn_id
            if not force and active is not None:
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    workflow_id,
                    "PLAN_NOT_IDLE",
                    "Plan mode can only be cancelled while the ROOT slot is idle.",
                )
            self._plan_exit_fence = True
        try:
            if force:
                # Phase one validates the exact workflow/revision before any
                # physical cancellation.  The same transaction interrupts the
                # exact canonical ROOT turn and aborts its interaction.
                await self._io.run(
                    self.repository.prepare_force_plan_exit,
                    self._lease.guard,
                    command_id=command_id,
                    workflow_id=workflow_id,
                    expected_workflow_revision=expected_workflow_revision,
                    expected_active_turn_id=active_turn_id,
                    occurred_at=datetime.now().astimezone(),
                    actor_id=self.host_session_id,
                    deadline_monotonic=self._canonical_deadline(),
                )
                if active is not None:
                    active.cancel()
                    try:
                        await active
                    except BaseException:
                        pass
                    await self._settle_active_root_task(active)
                await self._plan_interactions.abort_current(
                    RuntimeError("Plan question was aborted by force exit")
                )
                # The origin task may have detached from a shielded automatic
                # continuation after that continuation committed FULL.  Keep
                # the exit fence installed until every already-admitted owner
                # has either bound before the fence or interrupted its exact
                # unbound successor.
                await self._plan_continuations.drain()
            accepted = await self._io.run(
                self.repository.exit_plan_by_user,
                self._lease.guard,
                command_id=command_id,
                command_kind=command_kind,
                workflow_id=workflow_id,
                expected_workflow_revision=expected_workflow_revision,
                occurred_at=datetime.now().astimezone(),
                actor_id=self.host_session_id,
                deadline_monotonic=self._canonical_deadline(),
            )
            return _plan_workflow_command_outcome(accepted)
        finally:
            async with self._lock:
                self._plan_exit_fence = False
                self._queue_wake.set()
                self._monitor_wake.set()

    async def resolve_plan_question(
        self,
        *,
        command_id: str,
        workflow_id: str,
        expected_workflow_revision: int,
        interaction_id: str,
        answer: PlanQuestionAnswer,
        write_expected_writer_generation: int | None = None,
    ) -> AcceptedPlanResolution:
        self._require_open()
        result_id = _stable_id("plan-question-result", self.session_id, command_id)
        result_entry_id = _stable_id(
            "plan-question-result-entry", self.session_id, command_id
        )
        semantic_candidate_fingerprint = plan_question_resolution_semantic_fingerprint(
            workflow_id=workflow_id,
            expected_workflow_revision=expected_workflow_revision,
            interaction_id=interaction_id,
            answer=answer,
            result_id=result_id,
            result_entry_id=result_entry_id,
        )
        existing = await self._io.run(
            self.repository.confirm_plan_question_winner,
            session_id=self.session_id,
            command_id=command_id,
            workflow_id=workflow_id,
            expected_workflow_revision=expected_workflow_revision,
            interaction_id=interaction_id,
            answer=answer,
            result_id=result_id,
            result_entry_id=result_entry_id,
            deadline_monotonic=self._canonical_deadline(),
        )
        if existing is not None:
            await self._plan_interactions.settle(
                interaction_id=interaction_id,
                resolution=existing,
            )
            return existing
        if (
            write_expected_writer_generation is not None
            and write_expected_writer_generation != self.writer_generation
        ):
            raise ConversationKernelConflict(
                "Plan resolution writer generation is stale"
            )

        write_reservation: tuple[ModelInputScopeKind, str | None] | None = None

        async def resolve() -> AcceptedPlanResolution:
            try:
                try:
                    resolution = await self._io.run(
                        self.repository.resolve_plan_question,
                        self._lease.guard,
                        command_id=command_id,
                        workflow_id=workflow_id,
                        expected_workflow_revision=expected_workflow_revision,
                        interaction_id=interaction_id,
                        answer=answer,
                        result_id=result_id,
                        result_entry_id=result_entry_id,
                        occurred_at=datetime.now().astimezone(),
                        actor_id=self.host_session_id,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                except Exception:
                    # The same stable semantic candidate is retried only after its
                    # stateless query-first path has checked for a FULL winner.
                    resolution = await self._io.run(
                        self.repository.resolve_plan_question,
                        self._lease.guard,
                        command_id=command_id,
                        workflow_id=workflow_id,
                        expected_workflow_revision=expected_workflow_revision,
                        interaction_id=interaction_id,
                        answer=answer,
                        result_id=result_id,
                        result_entry_id=result_entry_id,
                        occurred_at=datetime.now().astimezone(),
                        actor_id=self.host_session_id,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                await self._plan_interactions.settle(
                    interaction_id=interaction_id,
                    resolution=resolution,
                )
                return resolution
            finally:
                if write_reservation is not None:
                    await self._release_compaction_write_reservation(write_reservation)

        def reserve_question_write() -> None:
            nonlocal write_reservation
            write_reservation = self._admit_plan_resolution_write_locked(
                creates_turn=False
            )

        async with self._lock:
            attempt = self._plan_continuations.start(
                attempt_id=f"plan-question-resolution:{command_id}",
                turn_id=f"question:{interaction_id}",
                semantic_candidate_fingerprint=semantic_candidate_fingerprint,
                run=resolve,
                before_start=reserve_question_write,
            )
        result = await asyncio.shield(attempt.task)
        if not isinstance(result, AcceptedPlanResolution):
            raise RuntimeError("Plan question resolution returned an invalid result")
        return result

    async def resolve_plan_draft_review(
        self,
        *,
        command_id: str,
        workflow_id: str,
        expected_workflow_revision: int,
        interaction_id: str,
        decision: PlanDraftDecision,
        feedback: str | None,
        write_expected_writer_generation: int | None = None,
    ) -> AcceptedPlanResolution:
        self._require_open()
        creates_turn = decision in {
            PlanDraftDecision.APPROVE,
            PlanDraftDecision.REVISE,
        }
        continuation_turn_id = (
            _stable_id("plan-review-turn", self.session_id, command_id)
            if creates_turn
            else None
        )
        continuation_entry_id = (
            _stable_id("plan-review-entry", self.session_id, command_id)
            if creates_turn
            else None
        )
        continuation_revision_id = (
            _stable_id("context-revision", continuation_turn_id or "", "0")
            if creates_turn
            else None
        )
        (
            normalized_feedback,
            _,
            semantic_candidate_fingerprint,
        ) = plan_draft_review_semantic_candidate(
            workflow_id=workflow_id,
            expected_workflow_revision=expected_workflow_revision,
            interaction_id=interaction_id,
            decision=decision,
            feedback=feedback,
            continuation_turn_id=continuation_turn_id,
            continuation_entry_id=continuation_entry_id,
            continuation_context_binding_revision_id=continuation_revision_id,
        )
        existing = await self._io.run(
            self.repository.confirm_plan_draft_review_winner,
            session_id=self.session_id,
            command_id=command_id,
            workflow_id=workflow_id,
            expected_workflow_revision=expected_workflow_revision,
            interaction_id=interaction_id,
            decision=decision,
            feedback=normalized_feedback,
            continuation_turn_id=continuation_turn_id,
            continuation_entry_id=continuation_entry_id,
            continuation_context_binding_revision_id=continuation_revision_id,
            deadline_monotonic=self._canonical_deadline(),
        )
        if existing is not None:
            return existing
        if (
            write_expected_writer_generation is not None
            and write_expected_writer_generation != self.writer_generation
        ):
            raise ConversationKernelConflict(
                "Plan resolution writer generation is stale"
            )
        reserved = False
        write_reservation: tuple[ModelInputScopeKind, str | None] | None = None
        attempt_id = f"plan-review-continuation:{command_id}"

        async def admit() -> AcceptedPlanResolution:
            try:
                try:
                    resolution = await self._io.run(
                        self.repository.resolve_plan_draft_review,
                        self._lease.guard,
                        command_id=command_id,
                        workflow_id=workflow_id,
                        expected_workflow_revision=expected_workflow_revision,
                        interaction_id=interaction_id,
                        decision=decision,
                        feedback=normalized_feedback,
                        continuation_turn_id=continuation_turn_id,
                        continuation_entry_id=continuation_entry_id,
                        continuation_context_binding_revision_id=(
                            continuation_revision_id
                        ),
                        occurred_at=datetime.now().astimezone(),
                        actor_id=self.host_session_id,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                except Exception:
                    resolution = await self._io.run(
                        self.repository.resolve_plan_draft_review,
                        self._lease.guard,
                        command_id=command_id,
                        workflow_id=workflow_id,
                        expected_workflow_revision=expected_workflow_revision,
                        interaction_id=interaction_id,
                        decision=decision,
                        feedback=normalized_feedback,
                        continuation_turn_id=continuation_turn_id,
                        continuation_entry_id=continuation_entry_id,
                        continuation_context_binding_revision_id=(
                            continuation_revision_id
                        ),
                        occurred_at=datetime.now().astimezone(),
                        actor_id=self.host_session_id,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                if resolution.continuation_turn_id is None:
                    if reserved:
                        await self._release_plan_continuation_reservation()
                    return resolution
                assert resolution.continuation_entry_id is not None
                handoff_kind = (
                    PlanHandoffKind.APPROVED_PLAN
                    if decision is PlanDraftDecision.APPROVE
                    else PlanHandoffKind.REVISION_REQUESTED
                )
                try:
                    inspection = await self._inspect_plan_continuation(
                        turn_id=resolution.continuation_turn_id,
                        entry_id=resolution.continuation_entry_id,
                        workflow_id=workflow_id,
                        interaction_id=interaction_id,
                        handoff_kind=handoff_kind,
                    )
                    disposition = self.repository.classify_plan_continuation(
                        inspection, self._lease.guard
                    )
                    if disposition is PlanContinuationDisposition.HISTORICAL_TERMINAL:
                        await self._release_plan_continuation_reservation()
                        return resolution
                    if await self._bind_plan_review_successor(
                        command_id=command_id,
                        inspection=inspection,
                    ):
                        return resolution
                except BaseException:
                    # FULL canonical acceptance already owns the successor;
                    # this attempt must continue into terminalization.
                    pass
                await self._terminalize_unbound_plan_successor(
                    attempt_id=attempt_id,
                    turn_id=resolution.continuation_turn_id,
                    entry_id=resolution.continuation_entry_id,
                    workflow_id=workflow_id,
                    interaction_id=interaction_id,
                    handoff_kind=handoff_kind,
                )
                await self._release_plan_continuation_reservation()
                return resolution
            except BaseException:
                if reserved:
                    await self._release_plan_continuation_reservation()
                raise
            finally:
                if write_reservation is not None:
                    await self._release_compaction_write_reservation(write_reservation)

        async with self._lock:

            def reserve_new_attempt() -> None:
                nonlocal reserved, write_reservation
                write_reservation = self._admit_plan_resolution_write_locked(
                    creates_turn=creates_turn
                )
                reserved = creates_turn

            attempt = self._plan_continuations.start(
                attempt_id=attempt_id,
                turn_id=continuation_turn_id or f"no-continuation:{command_id}",
                semantic_candidate_fingerprint=semantic_candidate_fingerprint,
                run=admit,
                before_start=reserve_new_attempt,
            )
        result = await asyncio.shield(attempt.task)
        if not isinstance(result, AcceptedPlanResolution):
            raise RuntimeError("Plan review admission returned an invalid result")
        return result

    async def _inspect_plan_continuation(
        self,
        *,
        turn_id: str,
        entry_id: str,
        workflow_id: str,
        interaction_id: str | None,
        handoff_kind: PlanHandoffKind,
    ) -> PlanContinuationInspection:
        inspection = await self._io.run(
            self.repository.inspect_plan_continuation,
            session_id=self.session_id,
            turn_id=turn_id,
            initial_entry_id=entry_id,
            workflow_id=workflow_id,
            interaction_id=interaction_id,
            handoff_kind=handoff_kind,
            deadline_monotonic=self._canonical_deadline(),
        )
        if inspection is None:
            raise ConversationKernelConflict("Plan continuation winner is absent")
        return inspection

    def _inspection_owned_by_current_host(
        self, inspection: PlanContinuationInspection
    ) -> bool:
        return (
            self.repository.classify_plan_continuation(inspection, self._lease.guard)
            is PlanContinuationDisposition.RUNNING_CURRENT_WRITER
        )

    async def _bind_plan_review_successor(
        self,
        *,
        command_id: str,
        inspection: PlanContinuationInspection,
    ) -> bool:
        async with self._lock:
            self._retire_done_active_root_locked()
            if (
                self._closing
                or self._plan_exit_fence
                or not self._inspection_owned_by_current_host(inspection)
                or not self._external_new_turn_accepting
                or self._active_task is not None
            ):
                return False
            self._tools.todo_owner.bind_continuation_if_present(
                scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
                turn_id=inspection.turn_id,
            )
            self._install_active_root_task_locked(
                turn_id=inspection.turn_id,
                command_id=command_id,
                name=f"kernel-plan-review-turn:{inspection.turn_id}",
                run=lambda intent: self._run_accepted_root_chain(
                    inspection.turn_id, intent
                ),
            )
            self._external_new_turn_accepting = False
            self._external_new_turn_settled.set()
            return True

    async def _terminalize_unbound_plan_successor(
        self,
        *,
        attempt_id: str,
        turn_id: str,
        entry_id: str,
        workflow_id: str,
        interaction_id: str | None,
        handoff_kind: PlanHandoffKind,
    ) -> None:
        """Retain ownership until the exact FULL successor is safely settled."""

        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Plan successor terminalization has no task owner")
        self._plan_continuations.mark_terminalizing(
            attempt_id=attempt_id,
            task=task,
        )
        await self._terminalize_plan_successor_until_safe(
            turn_id=turn_id,
            entry_id=entry_id,
            workflow_id=workflow_id,
            interaction_id=interaction_id,
            handoff_kind=handoff_kind,
            reason="PLAN_CONTINUATION_NOT_BOUND",
        )

    async def _terminalize_pending_root_successor(
        self, pending: _PendingRootSuccessorHandoff, *, reason: str
    ) -> None:
        await self._terminalize_plan_successor_until_safe(
            turn_id=pending.successor_turn_id,
            entry_id=pending.successor_entry_id,
            workflow_id=pending.workflow_id,
            interaction_id=pending.interaction_id,
            handoff_kind=pending.handoff_kind,
            reason=reason,
        )

    async def _terminalize_plan_successor_until_safe(
        self,
        *,
        turn_id: str,
        entry_id: str,
        workflow_id: str,
        interaction_id: str | None,
        handoff_kind: PlanHandoffKind,
        reason: str,
    ) -> None:
        delay_seconds = 0.05
        while True:
            try:
                await self._io.run(
                    self.repository.interrupt_turn,
                    self._lease.guard,
                    turn_id=turn_id,
                    reason=reason,
                    occurred_at=datetime.now().astimezone(),
                    actor_id=self.host_session_id,
                    deadline_monotonic=self._canonical_deadline(),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Confirmation below distinguishes a replacement writer from
                # a transient failure under this exact writer.
                pass
            try:
                inspection = await self._inspect_plan_continuation(
                    turn_id=turn_id,
                    entry_id=entry_id,
                    workflow_id=workflow_id,
                    interaction_id=interaction_id,
                    handoff_kind=handoff_kind,
                )
                disposition = self.repository.classify_plan_continuation(
                    inspection, self._lease.guard
                )
                if disposition in {
                    PlanContinuationDisposition.HISTORICAL_TERMINAL,
                    PlanContinuationDisposition.NOT_OWNED_BY_CURRENT_WRITER,
                }:
                    if disposition is PlanContinuationDisposition.HISTORICAL_TERMINAL:
                        self._memory_tools.offer_governance_wake()
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(delay_seconds)
            delay_seconds = min(delay_seconds * 2, 0.5)

    async def _release_plan_continuation_reservation(self) -> None:
        async with self._lock:
            self._external_new_turn_accepting = False
            self._external_new_turn_settled.set()

    async def _prompt_delivery_loop(self) -> None:
        while True:
            await self._queue_wake.wait()
            self._queue_wake.clear()
            if self._closing:
                return
            while not self._closing:
                async with self._lock:
                    self._retire_done_active_root_locked()
                    active = self._active_task
                    external_new_turn_accepting = self._external_new_turn_accepting
                    plan_exit_fence = self._plan_exit_fence
                    compaction_fenced = self._compaction.is_fenced(
                        scope_kind=ModelInputScopeKind.ROOT,
                        scope_subagent_task_id=None,
                    )
                if compaction_fenced:
                    await asyncio.sleep(0.05)
                    self._queue_wake.set()
                    break
                if plan_exit_fence:
                    await asyncio.sleep(0)
                    continue
                if external_new_turn_accepting:
                    await asyncio.sleep(0)
                    continue
                if active is not None:
                    try:
                        await asyncio.shield(active)
                    except BaseException:
                        pass
                    continue
                try:
                    candidate = await self._io.run(
                        self.repository.prepare_prompt_head_consumption,
                        session_id=self.session_id,
                        occurred_at=datetime.now().astimezone(),
                        actor_id=self.host_session_id,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await asyncio.sleep(0.1)
                    self._queue_wake.set()
                    break
                if candidate is None:
                    try:
                        head_mode = await self._io.run(
                            self.repository.pending_prompt_head_mode,
                            session_id=self.session_id,
                            deadline_monotonic=self._canonical_deadline(),
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        await asyncio.sleep(0.1)
                        self._queue_wake.set()
                        break
                    if head_mode is PromptDeliveryMode.NEW_TURN:
                        continue
                    break
                try:
                    model_resolution_snapshot = (
                        self._model_runtime.freeze_resolution_snapshot()
                    )
                except ModelRuntimeUnavailable:
                    # No trustworthy process-local catalog/settings cut exists.
                    # The canonical queue row stays pending until an ordinary
                    # refresh or settings repair wakes delivery again.
                    await asyncio.sleep(0.1)
                    self._queue_wake.set()
                    break
                try:
                    model_resolution_snapshot.validate(candidate.model_call_binding)
                except (KeyError, ValueError):
                    rejected = await self._settle_queued_model_rejection(candidate)
                    if rejected:
                        self._queue_wake.set()
                        continue
                    break
                async with self._lock:
                    try:
                        write_reservation = self._reserve_compaction_write_locked(
                            scope_kind=ModelInputScopeKind.ROOT,
                            scope_subagent_task_id=None,
                        )
                    except RuntimeError:
                        self._queue_wake.set()
                        break
                settlement = asyncio.create_task(
                    self._settle_queued_root_admission_reserved(
                        candidate, write_reservation
                    ),
                    name=f"kernel-queued-admission:{candidate.queue_item_id}",
                )
                try:
                    task = await asyncio.shield(settlement)
                except asyncio.CancelledError:
                    # Host close detaches the delivery-loop waiter, never the
                    # exact queue admission settlement owner.
                    while not settlement.done():
                        try:
                            await asyncio.shield(settlement)
                        except asyncio.CancelledError:
                            continue
                    settlement.result()
                    raise
                if task is None:
                    self._queue_wake.set()
                    break
                try:
                    await task
                except BaseException:
                    pass
                finally:
                    await self._settle_active_root_task(task)

    async def _settle_queued_model_rejection(
        self,
        candidate: PreparedQueuedRootTurnAdmission,
    ) -> bool:
        """Settle one permanent pre-delivery failure without detaching its ACK."""

        delay_seconds = 0.05
        while not self._closing:
            try:
                return await self._io.run(
                    self.repository.reject_prepared_prompt_head_model_unavailable,
                    self._lease.guard,
                    candidate=candidate,
                    deadline_monotonic=self._canonical_deadline(),
                )
            except asyncio.CancelledError:
                raise
            except StaleHostWriter:
                return False
            except Exception:
                await asyncio.sleep(delay_seconds)
                delay_seconds = min(delay_seconds * 2, 0.5)
        return False

    async def _settle_queued_root_admission_reserved(
        self,
        candidate: PreparedQueuedRootTurnAdmission,
        reservation: tuple[ModelInputScopeKind, str | None],
    ) -> asyncio.Task[KernelRunResult] | None:
        try:
            return await self._settle_queued_root_admission(candidate)
        finally:
            await self._release_compaction_write_reservation(reservation)

    async def _settle_queued_root_admission(
        self, candidate: PreparedQueuedRootTurnAdmission
    ) -> asyncio.Task[KernelRunResult] | None:
        """Own one queued mutation through confirmation and task installation."""

        activation = build_root_activation(
            session_id=candidate.session_id,
            admission_kind="QUEUED",
            queue_item_id=candidate.queue_item_id,
            queue_sequence=candidate.queue_sequence,
            exact_turn_id=candidate.exact_turn_id,
            exact_initial_entry_id=candidate.exact_initial_entry_id,
            exact_context_binding_revision_id=(
                candidate.exact_context_binding_revision_id
            ),
        )
        confirmation: QueuedRootTurnAdmissionConfirmation | None = None
        delay_seconds = 0.05
        while confirmation is None:
            if self._closing:
                return None
            try:
                confirmation = await self._io.run(
                    self.repository.consume_prepared_prompt_head,
                    self._lease.guard,
                    candidate=candidate,
                    deadline_monotonic=self._canonical_deadline(),
                )
                if confirmation is None:
                    return None
            except asyncio.CancelledError:
                raise
            except StaleHostWriter:
                return None
            except Exception:
                while True:
                    try:
                        confirmation = await self._io.run(
                            self.repository.confirm_prepared_prompt_head_consumption,
                            candidate=candidate,
                            deadline_monotonic=self._canonical_deadline(),
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        await asyncio.sleep(delay_seconds)
                        delay_seconds = min(delay_seconds * 2, 0.5)
                        continue
                    if (
                        confirmation.kind
                        is QueuedRootTurnAdmissionConfirmationKind.NONE
                    ):
                        confirmation = None
                    break
        if confirmation.kind is QueuedRootTurnAdmissionConfirmationKind.CONFLICT:
            return None
        if confirmation.kind is not QueuedRootTurnAdmissionConfirmationKind.FULL:
            raise RuntimeError("queued ROOT admission has an invalid disposition")
        accepted = confirmation.accepted
        assert accepted is not None
        closed: FrozenTodoCloseProjection | None = None
        finalization_failed = False
        async with self._lock:
            try:
                closed = self._finalize_todo_run_activation_locked(activation, accepted)
            except BaseException:
                finalization_failed = True
                task = None
            if not finalization_failed and not self._closing:
                try:
                    task = self._install_active_root_task_locked(
                        turn_id=accepted.turn_id,
                        command_id=None,
                        name=f"kernel-queued-turn:{accepted.turn_id}",
                        run=lambda intent: self._run_accepted_root_chain(
                            accepted.turn_id, intent
                        ),
                    )
                    self._hook_context.activate_prompt_candidate(
                        scope=self._hook_root_scope,
                        prompt_candidate_id=candidate.queue_item_id,
                    )
                except BaseException:
                    task = None
            elif finalization_failed:
                task = None
            else:
                task = None
        self._tools.offer_todo_close(closed)
        if task is None:
            self._hook_context.retire_prompt_candidate(
                scope=self._hook_root_scope,
                prompt_candidate_id=candidate.queue_item_id,
            )
            await self._interrupt_queued_admission_until_safe(
                turn_id=accepted.turn_id,
                reason=(
                    "SESSION_CLOSED"
                    if self._closing
                    else "FOREGROUND_EXECUTION_INTERRUPTED"
                ),
            )
            if not finalization_failed:
                self._tools.todo_owner.mark_root_idle(exact_turn_id=accepted.turn_id)
        return task

    async def _interrupt_queued_admission_until_safe(
        self, *, turn_id: str, reason: str
    ) -> None:
        delay_seconds = 0.05
        while True:
            try:
                await self._io.run(
                    self.repository.interrupt_turn,
                    self._lease.guard,
                    turn_id=turn_id,
                    reason=reason,
                    occurred_at=datetime.now().astimezone(),
                    actor_id=self.host_session_id,
                    deadline_monotonic=self._canonical_deadline(),
                )
            except StaleHostWriter:
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            try:
                status = await self._io.run(
                    self.repository.read_turn_status,
                    session_id=self.session_id,
                    turn_id=turn_id,
                    deadline_monotonic=self._canonical_deadline(),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                status = None
            if status is not None and status.value != "RUNNING":
                self._memory_tools.offer_governance_wake()
                return
            await asyncio.sleep(delay_seconds)
            delay_seconds = min(delay_seconds * 2, 0.5)

    async def _terminal_monitor_delivery_loop(self) -> None:
        """Install process-local monitor drafts through the Host safe point."""

        coordinator = self._tools.terminal_monitor_coordinator
        while True:
            await self._monitor_wake.wait()
            self._monitor_wake.clear()
            if self._closing:
                await self._release_any_terminal_new_turn_reservation()
                return
            delivered = 0
            while delivered < STAGE2_LIMITS.pending_prompt_hard_items:
                monitor_ids = coordinator.pending_monitor_ids()
                if not monitor_ids or self._closing:
                    break
                try:
                    prompt_head = await self._io.run(
                        self.repository.pending_prompt_head_mode,
                        session_id=self.session_id,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await asyncio.sleep(0.1)
                    self._monitor_wake.set()
                    break
                if prompt_head is not None:
                    # Human input owns the next canonical safe point.
                    self._queue_wake.set()
                    await asyncio.sleep(0.05)
                    self._monitor_wake.set()
                    break
                if self._compaction.is_fenced(
                    scope_kind=ModelInputScopeKind.ROOT,
                    scope_subagent_task_id=None,
                ):
                    await asyncio.sleep(0.05)
                    self._monitor_wake.set()
                    break
                monitor_id = monitor_ids[0]
                observation_id = coordinator.pending_observation_id(monitor_id)
                if observation_id is None:
                    continue
                attempt = coordinator.current_installation_attempt(monitor_id)
                target = None if attempt is None else attempt.target
                reserved_new_turn = False
                write_reservation: tuple[ModelInputScopeKind, str | None] | None = None
                if target is None:
                    async with self._lock:
                        self._retire_done_active_root_locked()
                        active = self._active_task
                        active_turn_id = self._active_turn_id
                        if active is not None and active_turn_id is not None:
                            try:
                                write_reservation = (
                                    self._reserve_compaction_write_locked(
                                        scope_kind=ModelInputScopeKind.ROOT,
                                        scope_subagent_task_id=None,
                                    )
                                )
                            except RuntimeError:
                                pass
                            else:
                                target = ExistingTurnInstallation(
                                    turn_id=active_turn_id,
                                    entry_id=_stable_id(
                                        "entry", self.session_id, observation_id
                                    ),
                                )
                        elif (
                            not self._plan_exit_fence
                            and not self._external_new_turn_accepting
                        ):
                            try:
                                write_reservation = (
                                    self._reserve_compaction_write_locked(
                                        scope_kind=ModelInputScopeKind.ROOT,
                                        scope_subagent_task_id=None,
                                    )
                                )
                            except RuntimeError:
                                pass
                            else:
                                self._external_new_turn_accepting = True
                                self._terminal_new_turn_observation_id = observation_id
                                self._external_new_turn_settled.clear()
                                reserved_new_turn = True
                                turn_id = _stable_id(
                                    "turn", self.session_id, observation_id
                                )
                                target = NewTurnInstallation(
                                    turn_id=turn_id,
                                    context_binding_revision_id=_stable_id(
                                        "context-revision", turn_id, "0"
                                    ),
                                    initial_entry_id=_stable_id(
                                        "entry", self.session_id, observation_id
                                    ),
                                )
                elif isinstance(target, NewTurnInstallation):
                    async with self._lock:
                        if (
                            self._terminal_new_turn_observation_id == observation_id
                            and self._external_new_turn_accepting
                        ):
                            try:
                                write_reservation = (
                                    self._reserve_compaction_write_locked(
                                        scope_kind=ModelInputScopeKind.ROOT,
                                        scope_subagent_task_id=None,
                                    )
                                )
                            except RuntimeError:
                                target = None
                            else:
                                reserved_new_turn = True
                        elif (
                            self._active_task is None
                            and not self._external_new_turn_accepting
                        ):
                            try:
                                write_reservation = (
                                    self._reserve_compaction_write_locked(
                                        scope_kind=ModelInputScopeKind.ROOT,
                                        scope_subagent_task_id=None,
                                    )
                                )
                            except RuntimeError:
                                target = None
                            else:
                                self._external_new_turn_accepting = True
                                self._terminal_new_turn_observation_id = observation_id
                                self._external_new_turn_settled.clear()
                                reserved_new_turn = True
                        else:
                            target = None
                else:
                    async with self._lock:
                        try:
                            write_reservation = self._reserve_compaction_write_locked(
                                scope_kind=ModelInputScopeKind.ROOT,
                                scope_subagent_task_id=None,
                            )
                        except RuntimeError:
                            target = None
                if target is None:
                    await asyncio.sleep(0.05)
                    self._monitor_wake.set()
                    break
                try:
                    accepted = await self._runner.install_terminal_observation(
                        coordinator=coordinator,
                        monitor_id=monitor_id,
                        target=target,
                        workspace_id=self.workspace.workspace_key,
                        actor_id=self.host_session_id,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                except asyncio.CancelledError:
                    if write_reservation is not None:
                        await self._release_compaction_write_reservation(
                            write_reservation
                        )
                    raise
                except (ConversationKernelConflict, ExternalSourceNotAtSafePoint):
                    if write_reservation is not None:
                        await self._release_compaction_write_reservation(
                            write_reservation
                        )
                    if reserved_new_turn:
                        await self._release_terminal_new_turn_reservation(
                            observation_id
                        )
                    await asyncio.sleep(0.05)
                    self._monitor_wake.set()
                    break
                except Exception:
                    if write_reservation is not None:
                        await self._release_compaction_write_reservation(
                            write_reservation
                        )
                    # The immutable attempt remains process-local and is
                    # exact-confirmed by the next pass before any re-write.
                    await asyncio.sleep(0.1)
                    self._monitor_wake.set()
                    break
                if write_reservation is not None:
                    await self._release_compaction_write_reservation(write_reservation)
                if accepted is None:
                    if reserved_new_turn:
                        await self._release_terminal_new_turn_reservation(
                            observation_id
                        )
                    continue
                delivered += 1
                if isinstance(target, NewTurnInstallation):
                    await self._start_terminal_observation_turn(
                        accepted.turn_id, observation_id
                    )
                    break
            if self._closing:
                await self._release_any_terminal_new_turn_reservation()
                return

    async def _release_terminal_new_turn_reservation(self, observation_id: str) -> None:
        async with self._lock:
            if self._terminal_new_turn_observation_id != observation_id:
                return
            self._terminal_new_turn_observation_id = None
            self._external_new_turn_accepting = False
            self._external_new_turn_settled.set()
            self._queue_wake.set()

    async def _release_any_terminal_new_turn_reservation(self) -> None:
        async with self._lock:
            if self._terminal_new_turn_observation_id is None:
                return
            self._terminal_new_turn_observation_id = None
            self._external_new_turn_accepting = False
            self._external_new_turn_settled.set()
            self._queue_wake.set()

    async def _start_terminal_observation_turn(
        self, turn_id: str, observation_id: str
    ) -> None:
        async with self._lock:
            self._retire_done_active_root_locked()
            if (
                self._terminal_new_turn_observation_id != observation_id
                or not self._external_new_turn_accepting
                or self._active_task is not None
            ):
                raise RuntimeError("terminal observation turn lost its local admission")
            self._tools.todo_owner.bind_continuation_if_present(
                scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
                turn_id=turn_id,
            )
            self._install_active_root_task_locked(
                turn_id=turn_id,
                command_id=None,
                name=f"kernel-terminal-observation-turn:{turn_id}",
                run=lambda intent: self._run_accepted_root_chain(turn_id, intent),
            )
            self._terminal_new_turn_observation_id = None
            self._external_new_turn_accepting = False
            self._external_new_turn_settled.set()

    async def _query_command_row(self, command_id: str):
        return await self._io.run(
            self.repository.query_command,
            session_id=self.session_id,
            command_id=command_id,
            deadline_monotonic=self._canonical_deadline(),
        )

    def control_admission_deadline_ms(self) -> int:
        """Return the current Host's bounded monotonic admission frontier."""

        return int(self._control_monotonic() * 1000) + CONTROL_ADMISSION_WINDOW_MS

    def active_root_turn_id(self) -> str | None:
        """Project the exact process-local ROOT target without mutating it."""

        task = self._active_task
        return self._active_turn_id if task is not None and not task.done() else None

    def _control_rejection(
        self,
        request: UserControlRequest,
        code: str,
        message: str,
    ) -> KernelCommandOutcome:
        return KernelCommandOutcome(
            request.command_id,
            "REJECTED",
            request.target.target_id,
            code,
            message,
            user_control=UserControlOutcome(
                operation=request.operation,
                session_id=request.session_id,
                host_session_id=request.host_session_id,
                target=request.target,
                accepted=False,
                execution=UserControlExecution.NOT_STARTED,
            ),
        )

    def _validate_new_control_request_locked(
        self, request: UserControlRequest
    ) -> KernelCommandOutcome | None:
        expected_target_kind = {
            UserControlOperation.STOP_ACTIVE_TURN: UserControlTargetKind.ROOT_TURN,
            UserControlOperation.CANCEL_SUBAGENT_TASK: (
                UserControlTargetKind.SUBAGENT_TASK
            ),
            UserControlOperation.TERMINATE_BACKGROUND_PROCESS: (
                UserControlTargetKind.BACKGROUND_PROCESS
            ),
        }[request.operation]
        if (
            not request.command_id
            or not request.session_id
            or not request.host_session_id
            or not request.target.target_id
            or request.target.kind is not expected_target_kind
        ):
            return self._control_rejection(
                request,
                "CONTROL_REQUEST_INVALID",
                "The exact control owner and target are required.",
            )
        if (
            request.session_id != self.session_id
            or request.host_session_id != self.host_session_id
        ):
            return self._control_rejection(
                request,
                "OWNER_UNAVAILABLE",
                "The requested Host owner is not current.",
            )
        if self._closing or self._closed:
            return self._control_rejection(
                request,
                "OWNER_UNAVAILABLE",
                "The requested Host owner is closing or closed.",
            )
        deadline_ms = parse_control_command_deadline_ms(request.command_id)
        now_ms = int(self._control_monotonic() * 1000)
        if deadline_ms is None or deadline_ms > now_ms + CONTROL_ADMISSION_WINDOW_MS:
            return self._control_rejection(
                request,
                "CONTROL_REQUEST_INVALID",
                "The control command identity is invalid.",
            )
        if now_ms >= deadline_ms:
            return self._control_rejection(
                request,
                "CONTROL_REQUEST_EXPIRED",
                "The control command admission deadline has expired.",
            )
        return None

    def _retire_control_attempts_locked(self) -> None:
        now_ms = int(self._control_monotonic() * 1000)
        for command_id, attempt in tuple(self._user_control_attempts.items()):
            if attempt.task is not None and not attempt.task.done():
                continue
            control = attempt.outcome.user_control
            feedback = None if control is None else control.feedback
            if feedback is not None and (
                feedback.canonical_status is FeedbackCanonicalStatus.PENDING
                or feedback.inclusion_status is FeedbackInclusionStatus.PENDING
                or (
                    feedback.inclusion_status is FeedbackInclusionStatus.INCLUDED
                    and not feedback.transport.invocation_attempted
                    and feedback.target_root_turn_id == self._active_turn_id
                    and self._active_task is not None
                    and not self._active_task.done()
                )
            ):
                continue
            if (
                attempt.retain_until_monotonic_ms is not None
                and now_ms >= attempt.retain_until_monotonic_ms
            ):
                self._user_control_attempts.pop(command_id, None)

    def _existing_control_outcome_locked(
        self, request: UserControlRequest
    ) -> KernelCommandOutcome | None:
        self._retire_control_attempts_locked()
        attempt = self._user_control_attempts.get(request.command_id)
        if attempt is None:
            return None
        if attempt.request != request:
            return self._control_rejection(
                request,
                "CONTROL_COMMAND_CONFLICT",
                "The control command identity is already bound to another request.",
            )
        return attempt.outcome

    def _install_control_attempt_locked(
        self,
        request: UserControlRequest,
        pending: KernelCommandOutcome,
        *,
        feedback_target_turn_id: str | None = None,
        process_at_admission: TerminalProcessInfo | None = None,
    ) -> _UserControlAttempt:
        attempt = _UserControlAttempt(
            request=request,
            outcome=pending,
            feedback_target_turn_id=feedback_target_turn_id,
            process_at_admission=process_at_admission,
        )
        self._user_control_attempts[request.command_id] = attempt
        return attempt

    def _finish_control_attempt_locked(
        self, attempt: _UserControlAttempt, outcome: KernelCommandOutcome
    ) -> None:
        current = self._user_control_attempts.get(attempt.request.command_id)
        if current is not attempt:
            return
        attempt.outcome = outcome
        finished_ms = int(self._control_monotonic() * 1000)
        attempt.finished_monotonic_ms = finished_ms
        deadline_ms = parse_control_command_deadline_ms(attempt.request.command_id)
        assert deadline_ms is not None
        attempt.retain_until_monotonic_ms = max(
            deadline_ms,
            finished_ms + CONTROL_RESULT_RETENTION_MS,
        )
        control = outcome.user_control
        feedback = None if control is None else control.feedback
        if (
            feedback is None
            or feedback.canonical_status is not FeedbackCanonicalStatus.PENDING
        ):
            attempt.feedback_candidate_ready.set()
        if (
            feedback is None
            or feedback.canonical_status is not FeedbackCanonicalStatus.PENDING
        ):
            attempt.feedback_canonical_settled.set()

    @staticmethod
    def _user_control_request_identity(
        request: KernelModelExecutionRequest,
    ) -> tuple[str, str, int]:
        return (
            request.turn_id,
            request.cut.context_binding_revision_id,
            request.model_call_index,
        )

    def _reconcile_control_feedback_observations_locked(
        self,
        attempt: _UserControlAttempt,
        feedback: UserControlFeedbackState,
    ) -> UserControlFeedbackState:
        if feedback.canonical_status is not FeedbackCanonicalStatus.ACCEPTED:
            return feedback
        if feedback.inclusion_status is FeedbackInclusionStatus.INCLUDED:
            if (
                feedback.context_binding_revision_id is None
                or feedback.model_call_index is None
            ):
                return feedback
            transport = attempt.transport_observations.get(
                (
                    feedback.target_root_turn_id or "",
                    feedback.context_binding_revision_id,
                    feedback.model_call_index,
                )
            )
            return (
                feedback
                if transport is None
                else replace(feedback, transport=transport)
            )
        if (
            feedback.inclusion_status is not FeedbackInclusionStatus.PENDING
            or feedback.entry_id is None
        ):
            return feedback
        if not attempt.provider_input_observations:
            return feedback
        request_identity = min(
            attempt.provider_input_observations,
            key=lambda value: (value[2], value[1]),
        )
        transport = attempt.transport_observations.get(
            request_identity, feedback.transport
        )
        return replace(
            feedback,
            inclusion_status=FeedbackInclusionStatus.INCLUDED,
            context_binding_revision_id=request_identity[1],
            model_call_index=request_identity[2],
            transport=transport,
            reason=None,
        )

    def _replace_control_feedback_locked(
        self,
        attempt: _UserControlAttempt,
        feedback: UserControlFeedbackState,
    ) -> None:
        control = attempt.outcome.user_control
        if control is None:
            raise RuntimeError("user-control attempt lacks typed outcome")
        feedback = self._reconcile_control_feedback_observations_locked(
            attempt, feedback
        )
        attempt.outcome = replace(
            attempt.outcome,
            user_control=replace(control, feedback=feedback),
        )
        if feedback.canonical_status is not FeedbackCanonicalStatus.PENDING:
            attempt.feedback_canonical_settled.set()
        feedback_owner_still_advancing = (
            feedback.canonical_status is FeedbackCanonicalStatus.PENDING
            or feedback.inclusion_status is FeedbackInclusionStatus.PENDING
            or (
                feedback.inclusion_status is FeedbackInclusionStatus.INCLUDED
                and not feedback.transport.invocation_attempted
                and feedback.target_root_turn_id == self._active_turn_id
                and self._active_task is not None
                and not self._active_task.done()
            )
        )
        if (
            attempt.finished_monotonic_ms is not None
            and not feedback_owner_still_advancing
        ):
            now_ms = int(self._control_monotonic() * 1000)
            attempt.retain_until_monotonic_ms = max(
                attempt.retain_until_monotonic_ms or 0,
                now_ms + CONTROL_RESULT_RETENTION_MS,
            )

    def _mark_feedback_target_ended_locked(self, turn_id: str) -> None:
        for attempt in self._user_control_attempts.values():
            if attempt.feedback_target_turn_id != turn_id:
                continue
            control = attempt.outcome.user_control
            feedback = None if control is None else control.feedback
            if feedback is None:
                continue
            canonical_status = feedback.canonical_status
            inclusion_status = feedback.inclusion_status
            reason = feedback.reason
            if canonical_status is FeedbackCanonicalStatus.PENDING:
                if attempt.feedback_commit_outcome_unknown:
                    # The immutable candidate may already be committed.  Its
                    # owner must exact-confirm before target closure can be
                    # interpreted as a failed canonical write.
                    continue
                canonical_status = FeedbackCanonicalStatus.FAILED
                inclusion_status = FeedbackInclusionStatus.NOT_APPLICABLE
                reason = "TARGET_CLOSED"
            elif (
                canonical_status is FeedbackCanonicalStatus.ACCEPTED
                and inclusion_status is FeedbackInclusionStatus.PENDING
            ):
                inclusion_status = FeedbackInclusionStatus.TARGET_ENDED_BEFORE_INCLUSION
                reason = "TARGET_CLOSED"
            self._replace_control_feedback_locked(
                attempt,
                replace(
                    feedback,
                    canonical_status=canonical_status,
                    inclusion_status=inclusion_status,
                    reason=reason,
                ),
            )

    def _root_feedback_attempts_locked(
        self, turn_id: str
    ) -> tuple[_UserControlAttempt, ...]:
        return tuple(
            attempt
            for attempt in self._user_control_attempts.values()
            if attempt.feedback_target_turn_id == turn_id
            and not attempt.normal_end_coordination_exhausted
            and attempt.outcome.user_control is not None
            and attempt.outcome.user_control.feedback is not None
        )

    async def _coordinate_user_control_feedback_at_root_completion(
        self, turn_id: str
    ) -> bool:
        """Fence a normal ROOT answer until bound control facts are ready.

        The existing foreground canonical watchdog bounds waiting for a physical
        control result.  Once an immutable feedback candidate exists, the
        runner keeps the turn open so that the safe-point owner can install it
        before the next provider request.  Idle sessions never enter this path.
        """

        while True:
            async with self._lock:
                sealed_turn_id = getattr(
                    self, "_control_completion_sealed_turn_id", None
                )
                if sealed_turn_id not in {None, turn_id}:
                    raise RuntimeError(
                        "another ROOT control-completion phase is still sealed"
                    )
                self._control_completion_sealed_turn_id = turn_id
                attempts = self._root_feedback_attempts_locked(turn_id)
                wait_events: list[asyncio.Event] = []
                deadlines: list[float] = []
                should_fence = False
                now = monotonic()
                for attempt in attempts:
                    control = attempt.outcome.user_control
                    assert control is not None and control.feedback is not None
                    feedback = control.feedback
                    if attempt.normal_end_coordination_deadline is None:
                        attempt.normal_end_coordination_deadline = (
                            self._canonical_deadline()
                        )
                    deadline = attempt.normal_end_coordination_deadline
                    if now >= deadline:
                        attempt.normal_end_coordination_exhausted = True
                        continue
                    if (
                        feedback.canonical_status is FeedbackCanonicalStatus.PENDING
                        and attempt.feedback_candidate is None
                    ):
                        wait_events.append(attempt.feedback_candidate_ready)
                        deadlines.append(deadline)
                    elif feedback.canonical_status is FeedbackCanonicalStatus.PENDING:
                        attempt.normal_end_coordination_fenced = True
                        should_fence = True
                    elif (
                        feedback.canonical_status is FeedbackCanonicalStatus.ACCEPTED
                        and feedback.inclusion_status is FeedbackInclusionStatus.PENDING
                    ):
                        # No future request can be observed while this reply is
                        # itself waiting to complete.  Keep the ROOT open so
                        # the runner prepares the exact next request instead
                        # of timing out on an inclusion event with no producer.
                        attempt.normal_end_coordination_fenced = True
                        should_fence = True
                if wait_events:
                    deadline = min(deadlines)
                elif should_fence:
                    return True
                else:
                    return False
            remaining = max(0.0, deadline - monotonic())
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(event.wait() for event in wait_events)),
                    timeout=remaining,
                )
            except TimeoutError:
                # Give callbacks already queued on this loop one final chance
                # before applying the existing watchdog boundary.
                await asyncio.sleep(0)
                async with self._lock:
                    now = monotonic()
                    for attempt in self._root_feedback_attempts_locked(turn_id):
                        attempt_deadline = attempt.normal_end_coordination_deadline
                        if attempt_deadline is not None and now >= attempt_deadline:
                            attempt.normal_end_coordination_exhausted = True

    async def _settle_user_control_feedback_root_completion(
        self, turn_id: str, *, turn_completed: bool
    ) -> None:
        """Settle the exact control-feedback admission seal with the answer."""

        async with self._lock:
            if getattr(self, "_control_completion_sealed_turn_id", None) != turn_id:
                return
            if not turn_completed:
                self._control_completion_sealed_turn_id = None

    async def _await_user_control_feedback_before_root_provider(
        self, turn_id: str
    ) -> None:
        """Wait for fenced candidates at the next legal input boundary."""

        while True:
            async with self._lock:
                waiting = tuple(
                    attempt
                    for attempt in self._root_feedback_attempts_locked(turn_id)
                    if attempt.normal_end_coordination_fenced
                    and attempt.outcome.user_control is not None
                    and attempt.outcome.user_control.feedback is not None
                    and attempt.outcome.user_control.feedback.canonical_status
                    is FeedbackCanonicalStatus.PENDING
                )
                if not waiting:
                    return
                deadline = min(
                    attempt.normal_end_coordination_deadline
                    for attempt in waiting
                    if attempt.normal_end_coordination_deadline is not None
                )
                events = tuple(
                    attempt.feedback_canonical_settled for attempt in waiting
                )
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(event.wait() for event in events)),
                    timeout=max(0.0, deadline - monotonic()),
                )
            except TimeoutError:
                await asyncio.sleep(0)
                async with self._lock:
                    now = monotonic()
                    for attempt in waiting:
                        if (
                            attempt.normal_end_coordination_deadline is not None
                            and now >= attempt.normal_end_coordination_deadline
                        ):
                            attempt.normal_end_coordination_exhausted = True
                return

    def _pending_control_outcome(
        self,
        request: UserControlRequest,
        *,
        feedback: UserControlFeedbackState | None = None,
    ) -> KernelCommandOutcome:
        return KernelCommandOutcome(
            request.command_id,
            "PENDING",
            request.target.target_id,
            "CONTROL_ACCEPTED",
            "The exact control request was accepted.",
            user_control=UserControlOutcome(
                operation=request.operation,
                session_id=request.session_id,
                host_session_id=request.host_session_id,
                target=request.target,
                accepted=True,
                execution=UserControlExecution.RUNNING,
                feedback=feedback,
            ),
        )

    async def request_stop_turn(
        self,
        *,
        command_id: str,
        expected_session_id: str,
        expected_host_session_id: str,
        target_turn_id: str,
    ) -> KernelCommandOutcome:
        request = UserControlRequest(
            UserControlOperation.STOP_ACTIVE_TURN,
            command_id,
            expected_session_id,
            expected_host_session_id,
            UserControlTarget(UserControlTargetKind.ROOT_TURN, target_turn_id),
        )
        async with self._lock:
            existing = self._existing_control_outcome_locked(request)
            if existing is not None:
                return existing
            rejection = self._validate_new_control_request_locked(request)
            if rejection is not None:
                return rejection
            self._retire_done_active_root_locked()
            task = self._active_task
            intent = self._active_cancellation_intent
            if (
                task is not None
                and not task.done()
                and self._active_turn_id == target_turn_id
            ):
                if intent is None:
                    raise RuntimeError("active ROOT task lacks cancellation intent")
                intent.install_cause(ForegroundCancellationCause.USER_REQUEST)
                pending = self._pending_control_outcome(request)
                attempt = self._install_control_attempt_locked(request, pending)
                attempt.task = asyncio.create_task(
                    self._execute_stop_turn_attempt(attempt, task),
                    name=f"kernel-user-stop:{target_turn_id}",
                )
                return pending
            active_drifted = self._active_turn_id is not None
        terminal = await self._io.run(
            self.repository.read_turn_terminal_outcome,
            session_id=self.session_id,
            turn_id=target_turn_id,
            deadline_monotonic=self._canonical_deadline(),
        )
        async with self._lock:
            existing = self._existing_control_outcome_locked(request)
            if existing is not None:
                return existing
            if terminal is None or not str(terminal.get("status") or ""):
                return self._control_rejection(
                    request,
                    "CONTROL_TARGET_NOT_CURRENT"
                    if active_drifted
                    else "CONTROL_TARGET_UNAVAILABLE",
                    "The target ROOT turn is not the current running turn.",
                )
            root = RootControlResult(
                str(terminal["status"]),
                str(terminal.get("terminal_reason") or "") or None,
            )
            outcome = KernelCommandOutcome(
                command_id,
                "SUCCEEDED",
                target_turn_id,
                "CONTROL_ALREADY_TERMINAL",
                "The target ROOT turn had already reached a terminal state.",
                user_control=UserControlOutcome(
                    request.operation,
                    request.session_id,
                    request.host_session_id,
                    request.target,
                    False,
                    UserControlExecution.FINISHED,
                    root=root,
                ),
            )
            attempt = self._install_control_attempt_locked(request, outcome)
            self._finish_control_attempt_locked(attempt, outcome)
            return outcome

    async def _execute_stop_turn_attempt(
        self, attempt: _UserControlAttempt, task: asyncio.Task[KernelRunResult]
    ) -> None:
        try:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await self._settle_active_root_task(task)
            terminal = await self._io.run(
                self.repository.read_turn_terminal_outcome,
                session_id=self.session_id,
                turn_id=attempt.request.target.target_id,
                deadline_monotonic=self._canonical_deadline(),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            outcome = KernelCommandOutcome(
                attempt.request.command_id,
                "FAILED",
                attempt.request.target.target_id,
                "CONTROL_FAILED",
                "The ROOT control owner failed before confirming interruption.",
                user_control=UserControlOutcome(
                    attempt.request.operation,
                    attempt.request.session_id,
                    attempt.request.host_session_id,
                    attempt.request.target,
                    True,
                    UserControlExecution.FINISHED,
                ),
            )
            async with self._lock:
                self._finish_control_attempt_locked(attempt, outcome)
            return
        root = RootControlResult(
            None if terminal is None else str(terminal.get("status") or "") or None,
            None
            if terminal is None
            else str(terminal.get("terminal_reason") or "") or None,
        )
        succeeded = root.status == "INTERRUPTED"
        outcome = KernelCommandOutcome(
            attempt.request.command_id,
            "SUCCEEDED" if succeeded else "FAILED",
            attempt.request.target.target_id,
            "ROOT_STOPPED" if succeeded else "CONTROL_FAILED",
            "The target ROOT turn was stopped."
            if succeeded
            else "The target ROOT turn did not confirm interruption.",
            user_control=UserControlOutcome(
                attempt.request.operation,
                attempt.request.session_id,
                attempt.request.host_session_id,
                attempt.request.target,
                True,
                UserControlExecution.FINISHED,
                root=root,
            ),
        )
        async with self._lock:
            self._finish_control_attempt_locked(attempt, outcome)

    async def request_cancel_subagent(
        self,
        *,
        command_id: str,
        expected_session_id: str,
        expected_host_session_id: str,
        task_id: str,
    ) -> KernelCommandOutcome:
        request = UserControlRequest(
            UserControlOperation.CANCEL_SUBAGENT_TASK,
            command_id,
            expected_session_id,
            expected_host_session_id,
            UserControlTarget(UserControlTargetKind.SUBAGENT_TASK, task_id),
        )
        async with self._lock:
            existing = self._existing_control_outcome_locked(request)
            if existing is not None:
                return existing
            rejection = self._validate_new_control_request_locked(request)
            if rejection is not None:
                return rejection
        preflight = await self._subagents.preflight_cancel_task(task_id)
        async with self._lock:
            existing = self._existing_control_outcome_locked(request)
            if existing is not None:
                return existing
            rejection = self._validate_new_control_request_locked(request)
            if rejection is not None:
                return rejection
            if preflight is not None:
                if (
                    preflight.disposition
                    is SubagentCancelDisposition.TARGET_UNAVAILABLE
                ):
                    return self._control_rejection(
                        request,
                        "CONTROL_TARGET_UNAVAILABLE",
                        "The subagent target is unavailable.",
                    )
                outcome = KernelCommandOutcome(
                    request.command_id,
                    "SUCCEEDED",
                    request.target.target_id,
                    "CONTROL_ALREADY_TERMINAL",
                    "The subagent task was already terminal.",
                    user_control=UserControlOutcome(
                        request.operation,
                        request.session_id,
                        request.host_session_id,
                        request.target,
                        False,
                        UserControlExecution.FINISHED,
                        subagent=SubagentControlResult(
                            preflight.disposition.value,
                            preflight.status,
                            preflight.reason,
                        ),
                    ),
                )
                attempt = self._install_control_attempt_locked(request, outcome)
                self._finish_control_attempt_locked(attempt, outcome)
                return outcome
            pending = self._pending_control_outcome(request)
            attempt = self._install_control_attempt_locked(request, pending)
            attempt.task = asyncio.create_task(
                self._execute_cancel_subagent_attempt(attempt),
                name=f"kernel-user-cancel-subagent:{task_id}",
            )
            return pending

    async def _execute_cancel_subagent_attempt(
        self, attempt: _UserControlAttempt
    ) -> None:
        try:
            result = await self._subagents.cancel_task(attempt.request.target.target_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            outcome = KernelCommandOutcome(
                attempt.request.command_id,
                "FAILED",
                attempt.request.target.target_id,
                "CONTROL_FAILED",
                "The subagent control owner failed before confirming cancellation.",
                user_control=UserControlOutcome(
                    attempt.request.operation,
                    attempt.request.session_id,
                    attempt.request.host_session_id,
                    attempt.request.target,
                    True,
                    UserControlExecution.FINISHED,
                ),
            )
            async with self._lock:
                self._finish_control_attempt_locked(attempt, outcome)
            return
        unavailable = result.disposition is SubagentCancelDisposition.TARGET_UNAVAILABLE
        already = result.disposition is SubagentCancelDisposition.ALREADY_TERMINAL
        outcome = KernelCommandOutcome(
            attempt.request.command_id,
            "REJECTED" if unavailable else "SUCCEEDED",
            attempt.request.target.target_id,
            "CONTROL_TARGET_UNAVAILABLE"
            if unavailable
            else "CONTROL_ALREADY_TERMINAL"
            if already
            else "SUBAGENT_CANCELLED",
            "The subagent target is unavailable."
            if unavailable
            else "The subagent task was already terminal."
            if already
            else "The subagent task was cancelled.",
            user_control=UserControlOutcome(
                attempt.request.operation,
                attempt.request.session_id,
                attempt.request.host_session_id,
                attempt.request.target,
                not unavailable and not already,
                UserControlExecution.FINISHED,
                subagent=SubagentControlResult(
                    result.disposition.value,
                    result.status,
                    result.reason,
                ),
            ),
        )
        async with self._lock:
            self._finish_control_attempt_locked(attempt, outcome)

    async def request_terminate_background_process(
        self,
        *,
        command_id: str,
        expected_session_id: str,
        expected_host_session_id: str,
        process_id: str,
    ) -> KernelCommandOutcome:
        request = UserControlRequest(
            UserControlOperation.TERMINATE_BACKGROUND_PROCESS,
            command_id,
            expected_session_id,
            expected_host_session_id,
            UserControlTarget(UserControlTargetKind.BACKGROUND_PROCESS, process_id),
        )
        async with self._lock:
            existing = self._existing_control_outcome_locked(request)
            if existing is not None:
                return existing
            rejection = self._validate_new_control_request_locked(request)
            if rejection is not None:
                return rejection
            process = next(
                (
                    item
                    for item in self._tools.list_background_terminal_processes()
                    if item.process_id == process_id
                ),
                None,
            )
            if process is None:
                return self._control_rejection(
                    request,
                    "CONTROL_TARGET_UNAVAILABLE",
                    "The background process is unavailable in this Host.",
                )
            self._retire_done_active_root_locked()
            active_feedback_target = (
                self._active_turn_id
                if self._active_task is not None and not self._active_task.done()
                else None
            )
            feedback_target = (
                None
                if active_feedback_target
                == getattr(self, "_control_completion_sealed_turn_id", None)
                else active_feedback_target
            )
            feedback_reason = (
                None
                if feedback_target is not None
                else "TARGET_CLOSED"
                if active_feedback_target is not None
                else "IDLE_AT_ADMISSION"
            )
            feedback = UserControlFeedbackState(
                FeedbackCanonicalStatus.PENDING
                if feedback_target is not None
                else FeedbackCanonicalStatus.NOT_REQUIRED,
                FeedbackInclusionStatus.PENDING
                if feedback_target is not None
                else FeedbackInclusionStatus.NOT_APPLICABLE,
                FeedbackOwnerAvailability.AVAILABLE,
                reason=feedback_reason,
                target_root_turn_id=feedback_target,
            )
            pending = self._pending_control_outcome(request, feedback=feedback)
            attempt = self._install_control_attempt_locked(
                request,
                pending,
                feedback_target_turn_id=feedback_target,
                process_at_admission=process,
            )
            attempt.task = asyncio.create_task(
                self._execute_terminate_process_attempt(attempt),
                name=f"kernel-user-terminate-process:{process_id}",
            )
            return pending

    def list_background_processes(
        self, *, expected_session_id: str, expected_host_session_id: str
    ) -> tuple[TerminalProcessInfo, ...]:
        self._require_open()
        if (
            expected_session_id != self.session_id
            or expected_host_session_id != self.host_session_id
        ):
            raise KeyError("background process owner is unavailable")
        return tuple(self._tools.list_background_terminal_processes())

    def read_background_process_log(
        self,
        *,
        expected_session_id: str,
        expected_host_session_id: str,
        process_id: str,
        maximum_chars: int,
        since_cursor: str | None,
    ):
        self._require_open()
        if (
            expected_session_id != self.session_id
            or expected_host_session_id != self.host_session_id
        ):
            raise KeyError("background process owner is unavailable")
        if not any(
            item.process_id == process_id
            for item in self._tools.list_background_terminal_processes()
        ):
            raise KeyError("background process is unavailable")
        return self._tools.read_background_terminal_log(
            process_id,
            maximum_chars=maximum_chars,
            since_cursor=since_cursor,
        )

    async def _execute_terminate_process_attempt(
        self, attempt: _UserControlAttempt
    ) -> None:
        try:
            monitor_result = (
                self._tools.terminal_monitor_coordinator.cancel_for_process(
                    attempt.request.target.target_id
                )
            )
        except Exception:
            monitor_result = None
            monitor_failure_detail = "The monitor owner failed during cancellation."
        else:
            monitor_failure_detail = None
        try:
            result = await self._io.run(
                self._tools.terminate_background_terminal_process,
                attempt.request.target.target_id,
                deadline_monotonic=self._canonical_deadline(),
            )
            process = ProcessControlResult(
                result.disposition.value,
                result.result.status.value,
                result.result.exit_code,
                result.physical_state,
                result.group_alive,
            )
            physical_failed = (
                result.disposition.value == "PHYSICAL_SETTLEMENT_INCOMPLETE"
            )
        except Exception as exc:
            process = ProcessControlResult(
                "OWNER_UNAVAILABLE",
                "unknown",
                None,
                "unknown",
                None,
            )
            physical_failed = True
            physical_detail = str(exc)
        else:
            physical_detail = None
        monitor = (
            None
            if monitor_result is None
            else MonitorControlResult(
                monitor_result.monitor_id,
                monitor_result.outcome.value,
                monitor_result.in_flight_observation_ids,
                monitor_result.detail,
            )
        )
        monitor_failed = monitor_failure_detail is not None or (
            monitor is not None and monitor.outcome == "FAILED"
        )
        new_control_attempt = (
            result.disposition.value != "ALREADY_TERMINAL"
            if physical_detail is None
            else True
        ) or monitor_result is not None
        feedback_required = (
            attempt.feedback_target_turn_id is not None and new_control_attempt
        )
        admission_control = attempt.outcome.user_control
        admission_feedback = (
            None if admission_control is None else admission_control.feedback
        )
        feedback = UserControlFeedbackState(
            FeedbackCanonicalStatus.PENDING
            if feedback_required
            else FeedbackCanonicalStatus.NOT_REQUIRED,
            FeedbackInclusionStatus.PENDING
            if feedback_required
            else FeedbackInclusionStatus.NOT_APPLICABLE,
            FeedbackOwnerAvailability.AVAILABLE,
            reason=(
                None
                if feedback_required
                else "NO_NEW_CONTROL_EFFECT"
                if not new_control_attempt
                else (
                    admission_feedback.reason
                    if admission_feedback is not None
                    and admission_feedback.reason is not None
                    else "IDLE_AT_ADMISSION"
                )
            ),
            target_root_turn_id=attempt.feedback_target_turn_id,
        )
        failed = physical_failed or monitor_failed
        outcome = KernelCommandOutcome(
            attempt.request.command_id,
            "FAILED" if failed else "SUCCEEDED",
            attempt.request.target.target_id,
            "CONTROL_PARTIAL_FAILURE"
            if failed
            else "CONTROL_ALREADY_TERMINAL"
            if not new_control_attempt
            else "BACKGROUND_CONTROL_COMPLETED",
            physical_detail
            or monitor_failure_detail
            or (monitor.detail if monitor_failed and monitor is not None else None)
            or (
                "The background process control completed."
                if not failed
                else "The background process control was only partially completed."
            ),
            user_control=UserControlOutcome(
                attempt.request.operation,
                attempt.request.session_id,
                attempt.request.host_session_id,
                attempt.request.target,
                new_control_attempt,
                UserControlExecution.FINISHED,
                process=process,
                monitor=monitor,
                feedback=feedback,
            ),
        )
        async with self._lock:
            self._finish_control_attempt_locked(attempt, outcome)
        if feedback_required:
            try:
                await self._install_user_control_feedback_for_attempt(
                    attempt,
                    process=process,
                    monitor=monitor,
                    public_code=outcome.public_code,
                    public_detail=outcome.public_message,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                async with self._lock:
                    control = attempt.outcome.user_control
                    feedback = None if control is None else control.feedback
                    if feedback is not None:
                        self._replace_control_feedback_locked(
                            attempt,
                            replace(
                                feedback,
                                canonical_status=(
                                    FeedbackCanonicalStatus.FAILED
                                    if feedback.canonical_status
                                    is FeedbackCanonicalStatus.PENDING
                                    else feedback.canonical_status
                                ),
                                inclusion_status=(
                                    FeedbackInclusionStatus.NOT_APPLICABLE
                                    if feedback.inclusion_status
                                    is FeedbackInclusionStatus.PENDING
                                    else feedback.inclusion_status
                                ),
                                reason=feedback.reason or "FEEDBACK_OWNER_FAILED",
                            ),
                        )

    async def _install_user_control_feedback_for_attempt(
        self,
        attempt: _UserControlAttempt,
        *,
        process: ProcessControlResult,
        monitor: MonitorControlResult | None,
        public_code: str,
        public_detail: str,
    ) -> None:
        target_turn_id = attempt.feedback_target_turn_id
        process_info = attempt.process_at_admission
        if target_turn_id is None or process_info is None:
            raise RuntimeError("feedback attempt lost its frozen target or process")
        candidate = UserControlFeedbackInstallationAttempt(
            session_id=self.session_id,
            workspace_id=self.workspace.workspace_key,
            writer_generation=self._lease.guard.writer_generation,
            target_root_turn_id=target_turn_id,
            entry_id=f"entry:user-control:{uuid4().hex}",
            content=UserControlFeedbackContentV1(
                command_id=attempt.request.command_id,
                session_id=self.session_id,
                host_session_id=self.host_session_id,
                process_id=process_info.process_id,
                command=process_info.command,
                cwd=process_info.cwd,
                origin_turn_id=process_info.origin.turn_id,
                origin_subagent_task_id=(process_info.origin.scope_subagent_task_id),
                target_root_turn_id=target_turn_id,
                process=UserControlProcessFact(
                    process.disposition,
                    process.status,
                    process.exit_code,
                    process.physical_state,
                    process.group_alive,
                ),
                monitor=(
                    None
                    if monitor is None
                    else UserControlMonitorFact(
                        monitor.monitor_id,
                        monitor.outcome,
                        monitor.in_flight_observation_ids,
                        monitor.detail,
                    )
                ),
                public_code=public_code,
                public_detail=public_detail,
            ),
            occurred_at=datetime.now().astimezone(),
            actor_id=self.host_session_id,
        )
        provider_text = project_user_control_feedback_for_provider(
            candidate.content.canonical_bytes()
        )
        async with self._lock:
            if (
                self._user_control_attempts.get(attempt.request.command_id)
                is not attempt
            ):
                return
            attempt.feedback_candidate = candidate
            attempt.feedback_provider_text = provider_text
            attempt.feedback_candidate_ready.set()
        delay_seconds = 0.02
        while True:
            target_ended_with_unknown_commit = False
            closing_with_unknown_commit = False
            async with self._lock:
                if self._closing or self._closed:
                    if attempt.feedback_commit_outcome_unknown:
                        closing_with_unknown_commit = True
                    else:
                        control = attempt.outcome.user_control
                        feedback = None if control is None else control.feedback
                        if feedback is not None:
                            self._replace_control_feedback_locked(
                                attempt,
                                replace(
                                    feedback,
                                    canonical_status=(
                                        FeedbackCanonicalStatus.UNKNOWN
                                        if feedback.canonical_status
                                        is FeedbackCanonicalStatus.PENDING
                                        else feedback.canonical_status
                                    ),
                                    inclusion_status=(
                                        FeedbackInclusionStatus.UNKNOWN
                                        if feedback.inclusion_status
                                        is FeedbackInclusionStatus.PENDING
                                        else feedback.inclusion_status
                                    ),
                                    owner_availability=(
                                        FeedbackOwnerAvailability.UNAVAILABLE
                                    ),
                                    reason=feedback.reason or "OWNER_LOST",
                                ),
                            )
                        return
                elif (
                    self._active_turn_id != target_turn_id
                    or self._active_task is None
                    or self._active_task.done()
                ):
                    if attempt.feedback_commit_outcome_unknown:
                        target_ended_with_unknown_commit = True
                    else:
                        self._mark_feedback_target_ended_locked(target_turn_id)
                        return
            if target_ended_with_unknown_commit or closing_with_unknown_commit:
                try:
                    confirmed = await self._runner.confirm_user_control_feedback(
                        attempt=candidate,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    async with self._lock:
                        control = attempt.outcome.user_control
                        feedback = None if control is None else control.feedback
                        if feedback is not None:
                            self._replace_control_feedback_locked(
                                attempt,
                                replace(
                                    feedback,
                                    canonical_status=(FeedbackCanonicalStatus.UNKNOWN),
                                    inclusion_status=(FeedbackInclusionStatus.UNKNOWN),
                                    owner_availability=(
                                        FeedbackOwnerAvailability.UNAVAILABLE
                                        if closing_with_unknown_commit
                                        else feedback.owner_availability
                                    ),
                                    reason="CANONICAL_COMMIT_UNKNOWN",
                                ),
                            )
                    return
                async with self._lock:
                    attempt.feedback_commit_outcome_unknown = False
                    control = attempt.outcome.user_control
                    feedback = None if control is None else control.feedback
                    if feedback is None:
                        raise RuntimeError("feedback confirmation lost typed state")
                    if confirmed is None:
                        self._replace_control_feedback_locked(
                            attempt,
                            replace(
                                feedback,
                                canonical_status=FeedbackCanonicalStatus.FAILED,
                                inclusion_status=(
                                    FeedbackInclusionStatus.NOT_APPLICABLE
                                ),
                                owner_availability=(
                                    FeedbackOwnerAvailability.UNAVAILABLE
                                    if closing_with_unknown_commit
                                    else feedback.owner_availability
                                ),
                                reason=(
                                    "OWNER_LOST"
                                    if closing_with_unknown_commit
                                    else "TARGET_CLOSED"
                                ),
                            ),
                        )
                    else:
                        self._replace_control_feedback_locked(
                            attempt,
                            replace(
                                feedback,
                                canonical_status=FeedbackCanonicalStatus.ACCEPTED,
                                inclusion_status=FeedbackInclusionStatus.PENDING,
                                entry_id=confirmed.entry_id,
                                reason=None,
                            ),
                        )
                        if closing_with_unknown_commit:
                            reconciled_control = attempt.outcome.user_control
                            reconciled = (
                                None
                                if reconciled_control is None
                                else reconciled_control.feedback
                            )
                            if reconciled is None:
                                raise RuntimeError(
                                    "feedback reconciliation lost typed state"
                                )
                            self._replace_control_feedback_locked(
                                attempt,
                                replace(
                                    reconciled,
                                    inclusion_status=(
                                        reconciled.inclusion_status
                                        if reconciled.inclusion_status
                                        is FeedbackInclusionStatus.INCLUDED
                                        else FeedbackInclusionStatus.UNKNOWN
                                    ),
                                    owner_availability=(
                                        FeedbackOwnerAvailability.UNAVAILABLE
                                    ),
                                    reason=(
                                        reconciled.reason
                                        if reconciled.inclusion_status
                                        is FeedbackInclusionStatus.INCLUDED
                                        else "OWNER_LOST"
                                    ),
                                ),
                            )
                        else:
                            self._mark_feedback_target_ended_locked(target_turn_id)
                return
            async with self._lock:
                attempt.feedback_install_attempted = True
                # Until the exact write owner answers, target closure cannot
                # infer rollback from the absence of a Host-local ACK.
                attempt.feedback_commit_outcome_unknown = True
            try:
                accepted = await self._runner.install_user_control_feedback(
                    attempt=candidate,
                    deadline_monotonic=self._canonical_deadline(),
                )
            except asyncio.CancelledError:
                raise
            except ExternalSourceNotAtSafePoint:
                async with self._lock:
                    attempt.feedback_commit_outcome_unknown = False
                await asyncio.sleep(delay_seconds)
                delay_seconds = min(delay_seconds * 2, 0.5)
                continue
            except ConversationKernelConflict as exc:
                async with self._lock:
                    attempt.feedback_commit_outcome_unknown = False
                if str(exc) == "turn is not at a provider safe point":
                    await asyncio.sleep(delay_seconds)
                    delay_seconds = min(delay_seconds * 2, 0.5)
                    continue
                async with self._lock:
                    control = attempt.outcome.user_control
                    feedback = None if control is None else control.feedback
                    if feedback is not None:
                        self._replace_control_feedback_locked(
                            attempt,
                            replace(
                                feedback,
                                canonical_status=FeedbackCanonicalStatus.FAILED,
                                inclusion_status=(
                                    FeedbackInclusionStatus.NOT_APPLICABLE
                                ),
                                reason="CANONICAL_REJECTED",
                            ),
                        )
                return
            except StaleHostWriter:
                async with self._lock:
                    attempt.feedback_commit_outcome_unknown = False
                    control = attempt.outcome.user_control
                    feedback = None if control is None else control.feedback
                    if feedback is not None:
                        self._replace_control_feedback_locked(
                            attempt,
                            replace(
                                feedback,
                                canonical_status=FeedbackCanonicalStatus.UNKNOWN,
                                inclusion_status=FeedbackInclusionStatus.UNKNOWN,
                                owner_availability=(
                                    FeedbackOwnerAvailability.UNAVAILABLE
                                ),
                                reason="OWNER_LOST",
                            ),
                        )
                return
            except (ValueError, TypeError, AssertionError):
                async with self._lock:
                    attempt.feedback_commit_outcome_unknown = False
                    control = attempt.outcome.user_control
                    feedback = None if control is None else control.feedback
                    if feedback is not None:
                        self._replace_control_feedback_locked(
                            attempt,
                            replace(
                                feedback,
                                canonical_status=FeedbackCanonicalStatus.FAILED,
                                inclusion_status=(
                                    FeedbackInclusionStatus.NOT_APPLICABLE
                                ),
                                reason="CANONICAL_REJECTED",
                            ),
                        )
                return
            except Exception:
                # The safe-point owner exact-confirms this same immutable
                # candidate before any retry, including an ambiguous commit.
                await asyncio.sleep(delay_seconds)
                delay_seconds = min(delay_seconds * 2, 0.5)
                continue
            async with self._lock:
                attempt.feedback_commit_outcome_unknown = False
                control = attempt.outcome.user_control
                feedback = None if control is None else control.feedback
                if feedback is None:
                    raise RuntimeError("feedback installation lost typed state")
                self._replace_control_feedback_locked(
                    attempt,
                    replace(
                        feedback,
                        canonical_status=FeedbackCanonicalStatus.ACCEPTED,
                        inclusion_status=FeedbackInclusionStatus.PENDING,
                        entry_id=accepted.entry_id,
                        reason=None,
                    ),
                )
                if (
                    self._active_turn_id != target_turn_id
                    or self._active_task is None
                    or self._active_task.done()
                ):
                    self._mark_feedback_target_ended_locked(target_turn_id)
            return

    def _observe_user_control_provider_input_install(
        self, request: KernelModelExecutionRequest
    ) -> None:
        self._event_loop.call_soon(
            lambda: asyncio.create_task(
                self._record_user_control_provider_input_install(request),
                name=f"kernel-user-control-provider-input:{request.turn_id}",
            )
        )

    async def _record_user_control_provider_input_install(
        self, request: KernelModelExecutionRequest
    ) -> None:
        if (
            request.session_id != self.session_id
            or request.compiled_input.canonical_input_identity.conversation_scope_kind
            is not ModelInputScopeKind.ROOT
        ):
            return
        async with self._lock:
            for attempt in self._user_control_attempts.values():
                control = attempt.outcome.user_control
                feedback = None if control is None else control.feedback
                candidate_entry_id = (
                    None
                    if attempt.feedback_candidate is None
                    else attempt.feedback_candidate.entry_id
                )
                if (
                    feedback is None
                    or feedback.canonical_status
                    not in {
                        FeedbackCanonicalStatus.PENDING,
                        FeedbackCanonicalStatus.ACCEPTED,
                    }
                    or feedback.inclusion_status is not FeedbackInclusionStatus.PENDING
                    or (feedback.entry_id or candidate_entry_id) is None
                    or attempt.feedback_provider_text is None
                    or feedback.target_root_turn_id != request.turn_id
                ):
                    continue
                exact_entry_id = feedback.entry_id or candidate_entry_id
                exact_matches = tuple(
                    message
                    for message, placement in zip(
                        request.compiled_input.messages,
                        request.compiled_input.message_placements,
                        strict=True,
                    )
                    if placement.origin_entry_id == exact_entry_id
                    and message.content == (attempt.feedback_provider_text,)
                )
                if len(exact_matches) != 1:
                    continue
                attempt.provider_input_observations.add(
                    self._user_control_request_identity(request)
                )
                self._replace_control_feedback_locked(
                    attempt,
                    feedback,
                )

    async def _record_user_control_provider_input_failure(
        self, turn_id: str, reason: str
    ) -> None:
        if reason not in {"INPUT_COMPILE_FAILED", "INPUT_ADMISSION_FAILED"}:
            raise ValueError("user-control provider-input failure reason is invalid")
        async with self._lock:
            for attempt in self._user_control_attempts.values():
                control = attempt.outcome.user_control
                feedback = None if control is None else control.feedback
                if (
                    feedback is None
                    or feedback.canonical_status is not FeedbackCanonicalStatus.ACCEPTED
                    or feedback.inclusion_status is not FeedbackInclusionStatus.PENDING
                    or feedback.target_root_turn_id != turn_id
                ):
                    continue
                self._replace_control_feedback_locked(
                    attempt,
                    replace(
                        feedback,
                        inclusion_status=FeedbackInclusionStatus.FAILED,
                        reason=reason,
                    ),
                )

    def _observe_user_control_transport_invocation(
        self,
        request: KernelModelExecutionRequest,
        succeeded: bool,
        detail: str | None,
    ) -> None:
        self._event_loop.call_soon(
            lambda: asyncio.create_task(
                self._record_user_control_transport_invocation(
                    request, succeeded=succeeded, detail=detail
                ),
                name=f"kernel-user-control-transport:{request.turn_id}",
            )
        )

    async def _record_user_control_transport_invocation(
        self,
        request: KernelModelExecutionRequest,
        *,
        succeeded: bool,
        detail: str | None,
    ) -> None:
        if (
            request.session_id != self.session_id
            or request.compiled_input.canonical_input_identity.conversation_scope_kind
            is not ModelInputScopeKind.ROOT
        ):
            return
        async with self._lock:
            for attempt in self._user_control_attempts.values():
                control = attempt.outcome.user_control
                feedback = None if control is None else control.feedback
                if (
                    feedback is None
                    or feedback.target_root_turn_id != request.turn_id
                    or (
                        attempt.feedback_candidate is None
                        and feedback.inclusion_status
                        is not FeedbackInclusionStatus.INCLUDED
                    )
                ):
                    continue
                request_identity = self._user_control_request_identity(request)
                if (
                    feedback.inclusion_status is FeedbackInclusionStatus.INCLUDED
                    and request_identity
                    != (
                        feedback.target_root_turn_id,
                        feedback.context_binding_revision_id,
                        feedback.model_call_index,
                    )
                ):
                    continue
                attempt.transport_observations[request_identity] = (
                    FeedbackTransportResult(
                        invocation_attempted=True,
                        invocation_succeeded=succeeded,
                        detail=detail,
                    )
                )
                self._replace_control_feedback_locked(
                    attempt,
                    feedback,
                )

    async def query_control_command(
        self, request: UserControlRequest
    ) -> KernelControlQueryResult:
        if (
            request.session_id != self.session_id
            or request.host_session_id != self.host_session_id
        ):
            return KernelControlQueryResult(ControlQueryStatus.OWNER_UNAVAILABLE, None)
        async with self._lock:
            self._retire_control_attempts_locked()
            attempt = self._user_control_attempts.get(request.command_id)
            if attempt is not None:
                if attempt.request != request:
                    return KernelControlQueryResult(
                        ControlQueryStatus.FOUND,
                        self._control_rejection(
                            request,
                            "CONTROL_COMMAND_CONFLICT",
                            "The control command identity conflicts.",
                        ),
                    )
                return KernelControlQueryResult(
                    ControlQueryStatus.FOUND, attempt.outcome
                )
        return KernelControlQueryResult(ControlQueryStatus.RESULT_UNAVAILABLE, None)

    async def query_command(self, command_id: str) -> KernelCommandOutcome | None:
        failure = self._command_failures.get(command_id)
        row = await self._query_command_row(command_id)
        if row is None:
            return failure
        command_kind = str(row.get("command_kind") or "")
        if command_kind in {"ENTER_PLAN", "CANCEL_PLAN", "FORCE_EXIT_PLAN"}:
            workflow_status = str(row.get("plan_workflow_status") or "")
            if not workflow_status:
                raise ConversationKernelConflict(
                    "Plan workflow command is partially installed"
                )
            return KernelCommandOutcome(
                command_id=command_id,
                status="SUCCEEDED",
                target_id=str(row.get("target_plan_workflow_id") or ""),
                public_code={
                    "ENTER_PLAN": "PLAN_ENTERED",
                    "CANCEL_PLAN": "PLAN_CANCELLED",
                    "FORCE_EXIT_PLAN": "PLAN_FORCE_EXITED",
                }[command_kind],
                public_message="Plan workflow transition accepted.",
                plan_workflow_status=workflow_status,
                resume_permission_mode=PermissionMode(
                    str(row["plan_resume_permission_mode"])
                ),
                handoff_created_at_commit=command_kind != "ENTER_PLAN",
                plan_workflow_revision=int(row["plan_workflow_revision"]),
            )
        if command_kind == "RESOLVE_PLAN_INTERACTION":
            interaction_status = str(row.get("plan_interaction_status") or "")
            workflow_status = str(row.get("interaction_workflow_status") or "")
            if not interaction_status or not workflow_status:
                raise ConversationKernelConflict(
                    "Plan resolution command is partially installed"
                )
            interaction_kind = str(row.get("plan_interaction_kind") or "")
            draft_decision = None
            if interaction_kind == "DRAFT_REVIEW":
                draft_decision = {
                    "APPROVED": PlanDraftDecision.APPROVE,
                    "REVISION_REQUESTED": PlanDraftDecision.REVISE,
                    "CANCELLED": PlanDraftDecision.CANCEL,
                }.get(interaction_status)
                if draft_decision is None:
                    raise ConversationKernelConflict(
                        "Plan draft resolution command has an invalid terminal status"
                    )
            continuation_turn_id = (
                str(row.get("plan_continuation_turn_id") or "") or None
            )
            if (
                draft_decision in {PlanDraftDecision.APPROVE, PlanDraftDecision.REVISE}
            ) != (continuation_turn_id is not None):
                raise ConversationKernelConflict(
                    "Plan resolution continuation identity is incomplete"
                )
            return KernelCommandOutcome(
                command_id=command_id,
                status="SUCCEEDED",
                target_id=str(row.get("target_plan_interaction_id") or ""),
                public_code=f"PLAN_INTERACTION_{interaction_status}",
                public_message="Plan interaction resolution accepted.",
                plan_workflow_status=workflow_status,
                resume_permission_mode=PermissionMode(
                    str(row["interaction_resume_permission_mode"])
                ),
                handoff_created_at_commit=interaction_status == "CANCELLED",
                plan_workflow_revision=int(row["interaction_workflow_revision"]),
                plan_draft_decision=draft_decision,
                plan_continuation_turn_id=continuation_turn_id,
            )
        if row.get("interaction_decision") is not None:
            decision = str(row["interaction_decision"])
            return KernelCommandOutcome(
                command_id,
                "SUCCEEDED",
                str(row.get("target_interaction_decision_id") or ""),
                f"INTERACTION_{decision}",
                "Interaction decision accepted.",
            )
        if row.get("command_kind") == "ACCEPT_SUBAGENT_COMPLETION":
            return KernelCommandOutcome(
                command_id,
                "SUCCEEDED",
                str(row.get("target_entry_id") or ""),
                "SUBAGENT_COMPLETION_DELIVERED",
                "The delegated task outcome was delivered to Pulsara.",
            )
        status = str(row.get("turn_status") or "")
        target = str(row.get("target_turn_id") or "")
        if row.get("target_queue_item_id") is not None:
            queue_status = str(row.get("queue_status") or "")
            prompt_delivery = KernelPromptDelivery(
                queue_item_id=str(row["target_queue_item_id"]),
                queue_status=queue_status,
                consumed_entry_id=(str(row.get("consumed_entry_id") or "") or None),
                delivery_mode=str(row.get("queue_delivery_mode") or ""),
            )
            target = str(row.get("consumed_turn_id") or row["target_queue_item_id"])
            status = str(row.get("consumed_turn_status") or "")
            if queue_status == "PENDING":
                return KernelCommandOutcome(
                    command_id,
                    "PENDING",
                    target,
                    "PROMPT_QUEUED",
                    "Prompt is queued.",
                    prompt_delivery=prompt_delivery,
                )
            if queue_status in {"CANCELLED", "REJECTED"}:
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    target,
                    str(row.get("queue_terminal_reason") or queue_status),
                    "The queued prompt was not delivered.",
                    prompt_delivery=prompt_delivery,
                )
            if queue_status == "CONSUMED" and not status:
                return KernelCommandOutcome(
                    command_id,
                    "PENDING",
                    target,
                    "PROMPT_CONSUMED",
                    "Prompt was accepted into a canonical turn.",
                    prompt_delivery=prompt_delivery,
                )
        if status == "COMPLETED":
            return KernelCommandOutcome(
                command_id,
                "SUCCEEDED",
                target,
                "TURN_COMPLETED",
                "Reply accepted.",
                prompt_delivery=(
                    prompt_delivery
                    if row.get("target_queue_item_id") is not None
                    else None
                ),
            )
        if status == "INTERRUPTED":
            return KernelCommandOutcome(
                command_id,
                "REJECTED",
                target,
                str(row.get("terminal_reason") or "TURN_INTERRUPTED"),
                "The turn was interrupted and will not be replayed.",
                prompt_delivery=(
                    prompt_delivery
                    if row.get("target_queue_item_id") is not None
                    else None
                ),
            )
        return KernelCommandOutcome(
            command_id,
            "PENDING",
            target,
            "TURN_RUNNING",
            "The turn is running.",
            prompt_delivery=(
                prompt_delivery if row.get("target_queue_item_id") is not None else None
            ),
        )

    def attach_controller(self, attachment_id: str) -> bool:
        self._require_open()
        return self._interactions.attach_controller(attachment_id)

    def has_controller_attachment(self, attachment_id: str) -> bool:
        """Validate the process-local Plan-content capability holder."""

        return self._interactions.is_current_controller(attachment_id)

    def _offer_presentation_notice(self, notice: str) -> None:
        """Bind an ephemeral product notice to the current controller only."""

        attachment_id = self._interactions.current_controller_id()
        if attachment_id is None:
            return
        self._presentation_notices.setdefault(attachment_id, []).append(notice)

    def take_presentation_notices(self, attachment_id: str) -> tuple[str, ...]:
        """Consume notices only from their exact live controller attachment."""

        if not self._interactions.is_current_controller(attachment_id):
            self._presentation_notices.pop(attachment_id, None)
            return ()
        return tuple(self._presentation_notices.pop(attachment_id, ()))

    async def controller_detached(self, attachment_id: str) -> None:
        self._presentation_notices.pop(attachment_id, None)
        await self._interactions.controller_detached(attachment_id)

    async def resolve_tool_interaction(
        self,
        *,
        expected_writer_generation: int,
        expected_owner_epoch: int,
        expected_live_revision: int,
        interaction_id: str,
        command_id: str,
        decision: str,
        actor_id: str,
    ) -> KernelCommandOutcome:
        self._require_open()
        accepted = await self._interactions.resolve_tool_interaction(
            expected_writer_generation=expected_writer_generation,
            expected_owner_epoch=expected_owner_epoch,
            expected_live_revision=expected_live_revision,
            interaction_id=interaction_id,
            command_id=command_id,
            decision=decision,
            actor_id=actor_id,
        )
        return KernelCommandOutcome(
            command_id,
            "SUCCEEDED",
            accepted.decision_id,
            f"INTERACTION_{accepted.decision}",
            "Interaction decision accepted.",
        )

    def read_capability_form(
        self,
        *,
        attachment_id: str,
        interaction_id: str,
        expected_owner_epoch: int,
        expected_live_revision: int,
    ):
        self._require_open()
        return self._interactions.current_capability_form(
            attachment_id=attachment_id,
            interaction_id=interaction_id,
            expected_owner_epoch=expected_owner_epoch,
            expected_live_revision=expected_live_revision,
        )

    async def resolve_capability_form(
        self,
        *,
        attachment_id: str,
        interaction_id: str,
        expected_owner_epoch: int,
        expected_live_revision: int,
        decision: str,
        submission: dict[str, object] | None = None,
    ) -> None:
        self._require_open()
        await self._interactions.resolve_capability_form(
            attachment_id=attachment_id,
            interaction_id=interaction_id,
            expected_owner_epoch=expected_owner_epoch,
            expected_live_revision=expected_live_revision,
            decision=decision,
            submission=submission,
        )

    async def accept_subagent_completion(
        self,
        *,
        command_id: str,
        target_turn_id: str | None,
        requested_permission_mode: PermissionMode | None,
        task_id: str,
        actor_id: str,
    ) -> KernelCommandOutcome:
        self._require_open()
        existing = await self.query_command(command_id)
        if existing is not None:
            return existing
        new_turn = target_turn_id is None
        if new_turn and requested_permission_mode is None:
            return KernelCommandOutcome(
                command_id,
                "REJECTED",
                task_id,
                "PERMISSION_MODE_REQUIRED",
                "A new ROOT turn requires an explicit permission mode.",
            )
        if not new_turn and requested_permission_mode is not None:
            return KernelCommandOutcome(
                command_id,
                "REJECTED",
                task_id,
                "PERMISSION_FIELD_NOT_ALLOWED",
                "An existing ROOT turn already owns its permission snapshot.",
            )
        resolved_turn_id = target_turn_id or _stable_id(
            "turn", self.session_id, command_id
        )
        new_revision_id = (
            _stable_id("context-revision", resolved_turn_id, "0") if new_turn else None
        )
        write_reservation: tuple[ModelInputScopeKind, str | None] | None = None
        if new_turn and not await self._reserve_external_new_turn():
            return KernelCommandOutcome(
                command_id,
                "REJECTED",
                task_id,
                "ROOT_TURN_ALREADY_RUNNING",
                "A ROOT turn is already running.",
            )
        if not new_turn:
            async with self._lock:
                try:
                    write_reservation = self._reserve_compaction_write_locked(
                        scope_kind=ModelInputScopeKind.ROOT,
                        scope_subagent_task_id=None,
                    )
                except RuntimeError:
                    return KernelCommandOutcome(
                        command_id,
                        "REJECTED",
                        task_id,
                        "COMPACTION_IN_PROGRESS",
                        "Context compaction is in progress for the ROOT scope.",
                    )
        try:
            accepted = await self._runner.accept_subagent_completion(
                turn_id=resolved_turn_id,
                new_context_binding_revision_id=new_revision_id,
                requested_permission_mode=requested_permission_mode,
                task_id=task_id,
                command_id=command_id,
                actor_id=actor_id,
                deadline_monotonic=self._canonical_deadline(),
            )
        except ExternalSourceNotAtSafePoint:
            if new_turn:
                await self._release_external_new_turn_reservation()
            return KernelCommandOutcome(
                command_id,
                "REJECTED",
                task_id,
                "PROVIDER_SAFE_POINT_REQUIRED",
                "The ROOT turn is currently dispatching a provider call.",
            )
        except BaseException as error:
            confirmed = await self._confirm_subagent_completion_command(
                command_id=command_id,
                command_kind="ACCEPT_SUBAGENT_COMPLETION",
                task_id=task_id,
            )
            if confirmed is not None:
                outcome, winner_turn_id = confirmed
                if new_turn and winner_turn_id == resolved_turn_id:
                    await self._start_subagent_completion_turn(
                        resolved_turn_id, command_id
                    )
                elif new_turn:
                    await self._release_external_new_turn_reservation()
                if isinstance(error, asyncio.CancelledError):
                    raise
                return outcome
            if new_turn:
                await self._release_external_new_turn_reservation()
            raise
        finally:
            if write_reservation is not None:
                await self._release_compaction_write_reservation(write_reservation)
        if accepted.disposition is SubagentCompletionDisposition.TARGET_STALE:
            if new_turn:
                await self._release_external_new_turn_reservation()
            return KernelCommandOutcome(
                command_id,
                "REJECTED",
                task_id,
                "SUBAGENT_COMPLETION_TARGET_STALE",
                "The conversation changed before this task outcome could be delivered.",
            )
        assert accepted.entry is not None
        if new_turn and accepted.disposition is SubagentCompletionDisposition.CREATED:
            await self._start_subagent_completion_turn(
                accepted.entry.turn_id, command_id
            )
        elif new_turn:
            await self._release_external_new_turn_reservation()
        return KernelCommandOutcome(
            command_id,
            "SUCCEEDED",
            accepted.entry.entry_id,
            "SUBAGENT_COMPLETION_DELIVERED",
            "The delegated task outcome was delivered to Pulsara.",
        )

    async def _reserve_external_new_turn(self) -> bool:
        async with self._lock:
            self._retire_done_active_root_locked()
            if (
                self._closing
                or self._plan_exit_fence
                or self._compaction.is_fenced(
                    scope_kind=ModelInputScopeKind.ROOT,
                    scope_subagent_task_id=None,
                )
                or self._external_new_turn_accepting
                or self._active_task is not None
            ):
                return False
            self._external_new_turn_accepting = True
            self._external_new_turn_settled.clear()
            return True

    async def _confirm_subagent_completion_command(
        self,
        *,
        command_id: str,
        command_kind: str,
        task_id: str,
    ) -> tuple[KernelCommandOutcome, str] | None:
        try:
            row = await self._query_command_row(command_id)
        except BaseException:
            return None
        if row is None or row.get("command_kind") != command_kind:
            return None
        observed_source = row.get("target_entry_source_subagent_task_id")
        if (
            observed_source != task_id
            or not row.get("target_entry_id")
            or not row.get("target_entry_turn_id")
        ):
            return None
        return (
            KernelCommandOutcome(
                command_id,
                "SUCCEEDED",
                str(row["target_entry_id"]),
                "SUBAGENT_COMPLETION_DELIVERED",
                "The delegated task outcome was delivered to Pulsara.",
            ),
            str(row["target_entry_turn_id"]),
        )

    async def _release_external_new_turn_reservation(self) -> None:
        async with self._lock:
            self._external_new_turn_accepting = False
            self._external_new_turn_settled.set()
            self._queue_wake.set()

    async def _start_subagent_completion_turn(
        self, turn_id: str, command_id: str
    ) -> None:
        async with self._lock:
            self._retire_done_active_root_locked()
            if not self._external_new_turn_accepting or self._active_task is not None:
                self._external_new_turn_accepting = False
                self._external_new_turn_settled.set()
                raise RuntimeError("subagent completion turn lost its local admission")
            self._tools.todo_owner.bind_continuation_if_present(
                scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
                turn_id=turn_id,
            )
            self._install_active_root_task_locked(
                turn_id=turn_id,
                command_id=command_id,
                name=f"kernel-subagent-completion-turn:{turn_id}",
                run=lambda intent: self._run_accepted_root_chain(turn_id, intent),
            )
            self._external_new_turn_accepting = False
            self._external_new_turn_settled.set()

    def request_close_conversation(self) -> None:
        """Monotonically merge the canonical-close bit into an installed close."""

        self._close_conversation_requested = True

    async def aclose(
        self,
        *,
        close_conversation: bool,
        deadline_monotonic: float | None = None,
        freeze_close_conversation_decision: (
            Callable[[], Awaitable[bool]] | None
        ) = None,
    ) -> None:
        async with self._close_async_lock:
            if self._closed:
                return
            deadline = (
                self._deadlines.deadline(KernelWatchdogOwner.HOST_SESSION_CLOSE)
                if deadline_monotonic is None
                else deadline_monotonic
            )
            async with self._lock:
                self._closing = True
                self._presentation_notices.clear()
                self._tools.todo_owner.mark_closing(
                    scope_kind=ModelInputScopeKind.ROOT,
                    scope_subagent_task_id=None,
                )
                self._close_conversation_requested = (
                    self._close_conversation_requested or close_conversation
                )
                self._queue_wake.set()
                self._monitor_wake.set()
            # SessionEnd consumes the exact close-attempt carrier.  Freeze the
            # current owner-held cwd before Terminal process termination can
            # retire that process-local owner; never reopen or query it from
            # the later terminal Hook lane.
            session_end_cwd = self._tools.snapshot_terminal_cwd()
            self._hooks.begin_host_close(deadline_monotonic=deadline)
            self.extensions.stop_admission()
            self._mcp_supervisor.stop_admission()
            mcp_close_task = asyncio.create_task(
                self._mcp_supervisor.aclose(),
                name=f"kernel-mcp-close:{self.host_session_id}",
            )
            close_error: BaseException | None = None
            self._delivery_task.cancel()
            try:
                if await _join_close_task(
                    self._delivery_task, deadline_monotonic=deadline
                ):
                    close_error = TimeoutError(
                        "prompt delivery settlement exited after close deadline"
                    )
            except BaseException as exc:
                close_error = exc
            try:
                if await _join_close_task(
                    self._monitor_task, deadline_monotonic=deadline
                ):
                    close_error = TimeoutError(
                        "terminal monitor scheduler exited after close deadline"
                    )
            except BaseException as exc:
                close_error = exc
            external_settlement = asyncio.create_task(
                self._external_new_turn_settled.wait(),
                name=f"kernel-external-settlement-close:{self.host_session_id}",
            )
            try:
                if await _join_close_task(
                    external_settlement, deadline_monotonic=deadline
                ):
                    close_error = close_error or TimeoutError(
                        "external-result admission settled after close deadline"
                    )
            except BaseException as exc:
                close_error = close_error or exc
            # A cancelled asyncio task cannot stop its Terminal worker thread.
            # Terminate and physically join monitor/process owners first so
            # the exact in-flight tool invocation can leave its bounded wait.
            try:
                await self._tools.stop_terminal_physical_owners(
                    timeout_seconds=max(0.001, deadline - monotonic())
                )
            except BaseException as exc:
                close_error = close_error or exc
            async with self._lock:
                task = self._active_task
                intent = self._active_cancellation_intent
                if task is not None and not task.done():
                    if intent is None:
                        raise RuntimeError("active ROOT task lacks cancellation intent")
                    intent.install_cause(ForegroundCancellationCause.HOST_SESSION_CLOSE)
            if task is not None and not task.done():
                task.cancel()
                try:
                    if await _join_close_task(task, deadline_monotonic=deadline):
                        close_error = close_error or TimeoutError(
                            "canonical foreground task exited after close deadline"
                        )
                except BaseException as exc:
                    close_error = close_error or exc
            if task is not None:
                await self._settle_active_root_task(task)
            async with self._lock:
                control_tasks = tuple(
                    attempt.task
                    for attempt in self._user_control_attempts.values()
                    if attempt.task is not None and not attempt.task.done()
                )
            for control_task in control_tasks:
                try:
                    if await _join_close_task(
                        control_task, deadline_monotonic=deadline
                    ):
                        close_error = close_error or TimeoutError(
                            "user-control settlement exited after close deadline"
                        )
                except BaseException as exc:
                    close_error = close_error or exc
            async with self._lock:
                for attempt in self._user_control_attempts.values():
                    control = attempt.outcome.user_control
                    feedback = None if control is None else control.feedback
                    if feedback is None:
                        continue
                    self._replace_control_feedback_locked(
                        attempt,
                        replace(
                            feedback,
                            canonical_status=(
                                FeedbackCanonicalStatus.UNKNOWN
                                if feedback.canonical_status
                                is FeedbackCanonicalStatus.PENDING
                                else feedback.canonical_status
                            ),
                            inclusion_status=(
                                FeedbackInclusionStatus.UNKNOWN
                                if feedback.inclusion_status
                                is FeedbackInclusionStatus.PENDING
                                else feedback.inclusion_status
                            ),
                            owner_availability=FeedbackOwnerAvailability.UNAVAILABLE,
                            reason=feedback.reason or "OWNER_LOST",
                        ),
                    )
            try:
                # The exact ROOT runner and its shielded ToolResult settlement
                # owner have already joined. A retained token now has no
                # legitimate producer that can advance it, so fail closed
                # instead of sleeping on an ownerless condition.
                todo_closed = self._tools.todo_owner.close_run(
                    scope_kind=ModelInputScopeKind.ROOT,
                    scope_subagent_task_id=None,
                )
            except BaseException as exc:
                todo_closed = None
                close_error = close_error or exc
            self._tools.offer_todo_close(todo_closed)
            self._renewal_task.cancel()
            try:
                await self._renewal_task
            except BaseException:
                pass
            for close_operation in (
                self._compaction.aclose,
                self._plan_interactions.aclose,
                self._plan_continuations.aclose,
                self._interactions.aclose,
            ):
                try:
                    await close_operation()
                except BaseException as exc:
                    close_error = close_error or exc
            try:
                await self._subagents.aclose(deadline_monotonic=deadline)
            except BaseException as exc:
                close_error = close_error or exc
            try:
                await self._assistant_settlements.aclose(deadline_monotonic=deadline)
            except BaseException as exc:
                close_error = close_error or exc
            try:
                terminal_view = await self._hooks.fence_ordinary()
                current_binding = await self._io.run(
                    self.repository.read_session_model_call_binding,
                    self._lease.guard,
                    deadline_monotonic=deadline,
                )
                terminal_input = SessionEndInput(
                    session_id=self.session_id,
                    cwd=str(session_end_cwd),
                    model=(
                        "unconfigured"
                        if current_binding is None
                        else self._model_identity(current_binding)
                    ),
                    reason="other",
                )
                await self._hooks.dispatch_terminal(
                    HookDispatchEnvelope(
                        terminal_view,
                        self._hook_root_scope,
                        terminal_input,
                        SessionEndRef(object()),
                        deadline,
                    ),
                    matcher_subject=event_matcher_subject(
                        terminal_input.event_type, reason=terminal_input.reason
                    ),
                )
            except BaseException as exc:
                close_error = close_error or exc
            try:
                await self._hooks.aclose(deadline_monotonic=deadline)
            except BaseException as exc:
                close_error = close_error or exc
            self._hook_context.close()
            self._input_continuity.close()
            try:
                await self._memory_governor.aclose(deadline_monotonic=deadline)
            except BaseException as exc:
                close_error = close_error or exc
            try:
                await self._memory_tools.aclose()
            except BaseException as exc:
                close_error = close_error or exc
            try:
                await self._tools.aclose(
                    timeout_seconds=max(0.001, deadline - monotonic())
                )
            except BaseException as exc:
                close_error = close_error or exc
            try:
                if await _join_close_task(mcp_close_task, deadline_monotonic=deadline):
                    close_error = close_error or TimeoutError(
                        "MCP physical owners exited after close deadline"
                    )
            except BaseException as exc:
                close_error = close_error or exc
            try:
                self._plugin_view.close()
                self._plugin_view = FrozenEnabledPluginView(
                    EnabledPluginViewDisposition.COMPLETE
                )
            except BaseException as exc:
                close_error = close_error or exc
            try:
                await self.extensions.aclose(deadline_monotonic=deadline)
            except BaseException as exc:
                close_error = close_error or exc
            canonical_close = self._close_conversation_requested
            if freeze_close_conversation_decision is not None:
                try:
                    canonical_close = await freeze_close_conversation_decision()
                except BaseException as exc:
                    close_error = close_error or exc
                    canonical_close = False
            if canonical_close:
                try:
                    await self._io.run(
                        self.repository.close_session,
                        self._lease.guard,
                        deadline_monotonic=deadline,
                    )
                except BaseException as exc:
                    close_error = close_error or exc
            self.live_bus.close()
            self.live_control.close()
            try:
                await self._io.aclose(deadline_monotonic=deadline)
            except BaseException as exc:
                close_error = close_error or exc
            self._closed = True
            if close_error is not None:
                raise close_error

    async def _renew_writer(self) -> None:
        while True:
            await asyncio.sleep(self._deadlines.policy.writer_renew_interval_seconds)
            self._lease = await self._io.run(
                self.repository.renew_host_writer,
                self._lease.guard,
                lease_seconds=self._deadlines.policy.writer_lease_seconds,
                memory_domain_id=self._memory_domain_id,
                deadline_monotonic=self._deadlines.deadline(
                    KernelWatchdogOwner.WRITER_RENEWAL
                ),
            )

    def _new_child_runner(
        self, hook_scope: HookDispatchScopeRef | None
    ) -> ConversationKernelRunner:
        return ConversationKernelRunner(
            repository=self.repository,
            writer_lease=self._lease,
            model=self._model,
            tools=self._tools,
            live_bus=self.live_bus,
            before_provider_preparation=self._adopt_capabilities_if_requested,
            io_owner=self._io,
            context_source_collector=self._context_sources,
            model_resolution_snapshot_provider=(
                self._model_runtime.freeze_resolution_snapshot
            ),
            continuity_owner=self._input_continuity,
            extensions=self.extensions,
            workspace_id=self.workspace.workspace_key,
            launch_permission_mode=self._launch_permission_mode,
            deadline_factory=self._deadlines,
            assistant_settlement_owner=self._assistant_settlements,
            todo_admission_finalizer=self._finalize_todo_run_activation,
            compaction_owner=self._compaction,
            subagent_runtime=self._subagents,
            hook_dispatcher=self._hooks,
            hook_context_owner=self._hook_context,
            hook_scope=hook_scope,
        )

    def _observe_provider_usage(
        self,
        request: KernelModelExecutionRequest,
        report: TransportUsageReport,
    ) -> None:
        usage = report.usage
        self.extensions.offer_operational_nowait(
            OperationalHookOffer(
                event_type=OperationalHookType.PROVIDER_USAGE_OBSERVED,
                session_id=request.session_id,
                turn_id=request.turn_id,
                public_payload={
                    "turn_id": request.turn_id,
                    "model_call_index": request.model_call_index,
                    "usage_status": report.usage_status,
                    "input_tokens": None if usage is None else usage.input_tokens,
                    "cached_input_tokens": (
                        None if usage is None else usage.cached_input_tokens
                    ),
                    "output_tokens": None if usage is None else usage.output_tokens,
                    "reasoning_output_tokens": (
                        None if usage is None else usage.reasoning_output_tokens
                    ),
                    "total_tokens": None if usage is None else usage.total_tokens,
                    "reported_model_id": report.reported_model_id,
                    "diagnostic_codes": tuple(
                        item.code for item in report.provider_diagnostics
                    ),
                },
            )
        )

    def _require_open(self) -> None:
        if self._closing:
            raise RuntimeError("kernel Host session is closing")


async def _join_close_task(
    task: asyncio.Task[object], *, deadline_monotonic: float
) -> bool:
    """Join the exact async owner; report if its logical close deadline elapsed."""

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
            # Only the waiter is cancelled.  The Host-owned close operation
            # keeps the physical owner attached until it is terminal.
            continue
        except BaseException:
            break
    if not task.cancelled():
        task.result()
    return deadline_expired


async def _join_task_beyond_logical_deadline(
    task: asyncio.Task[object],
    *,
    deadline_monotonic: float,
) -> tuple[bool, asyncio.CancelledError | None, BaseException | None]:
    """Join one physical task even after its logical close deadline expires.

    The deadline controls the caller-visible close outcome; it never transfers
    or abandons ownership of the task.  Cancellation likewise detaches only the
    current waiter and is re-raised after the exact physical owner has exited.
    """

    deadline_expired = False
    waiter_cancellation: asyncio.CancelledError | None = None
    while not task.done():
        remaining = deadline_monotonic - monotonic()
        if remaining > 0 and not deadline_expired:
            try:
                done, _pending = await asyncio.wait((task,), timeout=remaining)
            except asyncio.CancelledError as exc:
                waiter_cancellation = waiter_cancellation or exc
                continue
            if not done:
                deadline_expired = True
            continue
        deadline_expired = True
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if task.done():
                break
            waiter_cancellation = waiter_cancellation or exc
            continue
        except BaseException:
            break

    task_error: BaseException | None = None
    if not task.cancelled():
        try:
            task.result()
        except BaseException as exc:
            task_error = exc
    return deadline_expired, waiter_cancellation, task_error


def _stable_id(namespace: str, *parts: str) -> str:
    digest = sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"{namespace}:{digest}"


def _validate_prompt(value: str) -> None:
    encoded = value.encode("utf-8")
    if not encoded or len(encoded) > MAXIMUM_PROMPT_BYTES:
        raise ValueError("prompt is outside its finite byte bound")
    if any(character not in "\n\t" and ord(character) < 0x20 for character in value):
        raise ValueError("prompt contains a forbidden control character")


def _list_resumable_session_rows(
    repository: ConversationKernelRepository,
    workspace_id: str,
    memory_domain_id: str,
    include_closed: bool,
    limit: int,
    deadline_monotonic: float,
):
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=deadline_monotonic,
        isolation_level=IsolationLevel.REPEATABLE_READ,
    ) as connection:
        return connection.execute(
            """
            SELECT s.id, s.workspace_id, s.workspace_kind, s.workspace_root,
                   s.workspace_label, s.memory_domain_id, s.lifecycle,
                   s.writer_generation, s.latest_entry_sequence,
                   GREATEST(s.created_at, (
                       SELECT e.accepted_at
                       FROM pulsara_v3.transcript_entries AS e
                       WHERE e.session_id = s.id
                       ORDER BY e.entry_sequence DESC
                       LIMIT 1
                   )) AS updated_at,
                   s.model_call_binding,
                   count(t.id) AS subagent_task_total,
                   count(t.id) FILTER (WHERE t.status = 'ACTIVE')
                       AS subagent_task_active,
                   count(t.id) FILTER (WHERE t.status IN (
                       'PENDING_START', 'WAITING_DEPENDENCY'
                   )) AS subagent_task_waiting,
                   count(t.id) FILTER (WHERE t.status IN (
                       'FAILED', 'INTERRUPTED', 'BLOCKED_DEPENDENCY_FAILED'
                   )) AS subagent_task_attention
            FROM pulsara_v3.sessions AS s
            LEFT JOIN pulsara_v3.subagent_tasks AS t ON t.session_id = s.id
            WHERE s.workspace_id = %s AND s.memory_domain_id = %s
              AND (%s OR s.lifecycle = 'OPEN')
            GROUP BY s.id
            -- sessions.updated_at includes writer-lease maintenance.  Order the
            -- user-facing list by canonical conversation activity instead.
            ORDER BY updated_at DESC, s.id LIMIT %s
            """,
            (workspace_id, memory_domain_id, include_closed, limit),
        ).fetchall()


def _list_resumable_session_rows_across_workspaces(
    repository: ConversationKernelRepository,
    memory_domain_id: str,
    include_closed: bool,
    deadline_monotonic: float,
):
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=deadline_monotonic,
        isolation_level=IsolationLevel.REPEATABLE_READ,
    ) as connection:
        return connection.execute(
            """
            SELECT s.id, s.workspace_id, s.workspace_kind, s.workspace_root,
                   s.workspace_label, s.memory_domain_id, s.lifecycle,
                   s.writer_generation, s.latest_entry_sequence,
                   GREATEST(s.created_at, (
                       SELECT e.accepted_at
                       FROM pulsara_v3.transcript_entries AS e
                       WHERE e.session_id = s.id
                       ORDER BY e.entry_sequence DESC
                       LIMIT 1
                   )) AS updated_at,
                   s.model_call_binding,
                   count(t.id) AS subagent_task_total,
                   count(t.id) FILTER (WHERE t.status = 'ACTIVE')
                       AS subagent_task_active,
                   count(t.id) FILTER (WHERE t.status IN (
                       'PENDING_START', 'WAITING_DEPENDENCY'
                   )) AS subagent_task_waiting,
                   count(t.id) FILTER (WHERE t.status IN (
                       'FAILED', 'INTERRUPTED', 'BLOCKED_DEPENDENCY_FAILED'
                   )) AS subagent_task_attention
            FROM pulsara_v3.sessions AS s
            LEFT JOIN pulsara_v3.subagent_tasks AS t ON t.session_id = s.id
            WHERE s.memory_domain_id = %s AND (%s OR s.lifecycle = 'OPEN')
            GROUP BY s.id
            -- sessions.updated_at includes writer-lease maintenance.  Order the
            -- user-facing list by canonical conversation activity instead.
            ORDER BY updated_at DESC, s.id
            """,
            (memory_domain_id, include_closed),
        ).fetchall()


def _read_resumable_session_row(
    repository: ConversationKernelRepository,
    session_id: str,
    memory_domain_id: str,
    include_closed: bool,
    deadline_monotonic: float,
):
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=deadline_monotonic,
        isolation_level=IsolationLevel.REPEATABLE_READ,
    ) as connection:
        return connection.execute(
            """
            SELECT s.id, s.workspace_id, s.workspace_kind, s.workspace_root,
                   s.workspace_label, s.memory_domain_id, s.lifecycle,
                   s.writer_generation, s.latest_entry_sequence,
                   GREATEST(s.created_at, (SELECT e.accepted_at
                       FROM pulsara_v3.transcript_entries AS e WHERE e.session_id = s.id
                       ORDER BY e.entry_sequence DESC LIMIT 1)) AS updated_at,
                   s.model_call_binding,
                   count(t.id) AS subagent_task_total,
                   count(t.id) FILTER (WHERE t.status = 'ACTIVE')
                       AS subagent_task_active,
                   count(t.id) FILTER (WHERE t.status IN (
                       'PENDING_START', 'WAITING_DEPENDENCY'
                   )) AS subagent_task_waiting,
                   count(t.id) FILTER (WHERE t.status IN (
                       'FAILED', 'INTERRUPTED', 'BLOCKED_DEPENDENCY_FAILED'
                   )) AS subagent_task_attention
            FROM pulsara_v3.sessions AS s
            LEFT JOIN pulsara_v3.subagent_tasks AS t ON t.session_id = s.id
            WHERE s.id = %s AND s.memory_domain_id = %s
              AND (%s OR s.lifecycle = 'OPEN')
            GROUP BY s.id
            """,
            (session_id, memory_domain_id, include_closed),
        ).fetchone()


def _kernel_session_summary(row) -> KernelSessionSummary:
    return KernelSessionSummary(
        session_id=str(row["id"]),
        workspace_id=str(row["workspace_id"]),
        workspace_kind=str(row["workspace_kind"]),
        workspace_root=str(row["workspace_root"]),
        workspace_label=str(row["workspace_label"]),
        memory_domain_id=str(row["memory_domain_id"]),
        lifecycle=str(row["lifecycle"]),
        writer_generation=int(row["writer_generation"]),
        latest_entry_sequence=int(row["latest_entry_sequence"]),
        updated_at=row["updated_at"],
        model_call_binding=model_call_binding_from_dict(row["model_call_binding"]),
        subagent_task_total=int(row.get("subagent_task_total") or 0),
        subagent_task_active=int(row.get("subagent_task_active") or 0),
        subagent_task_waiting=int(row.get("subagent_task_waiting") or 0),
        subagent_task_attention=int(row.get("subagent_task_attention") or 0),
    )


class KernelHostCore:
    """Process owner for verified PostgreSQL and live Host sessions."""

    def __init__(
        self,
        *,
        model_runtime: ModelRuntime,
        authenticated_first_party_extension_ids: frozenset[str] = frozenset(),
        watchdog_policy: KernelExecutionWatchdogPolicy | None = None,
        credential_boundary: ProcessCredentialBoundary | None = None,
    ) -> None:
        self._model_runtime = model_runtime
        self._deadlines = KernelExecutionDeadlineFactory(
            watchdog_policy or DEFAULT_KERNEL_WATCHDOG_POLICY
        )
        self._access: VerifiedPostgresAccessLease | None = None
        self._repository: ConversationKernelRepository | None = None
        self._blob_store: PostgresCanonicalBlobStore | None = None
        self._blob_gc_io: KernelSessionIO | None = None
        self._blob_gc_task: asyncio.Task[None] | None = None
        self._sessions: dict[str, KernelHostSession] = {}
        self._workspace_capability_revisions: dict[Path, int] = {}
        self._open_attempts: set[asyncio.Future[None]] = set()
        self._close_attempts: dict[str, HostSessionCloseAttempt] = {}
        self._extension_routes: dict[str, tuple[str, KernelExtensionHost]] = {}
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()
        self._closing = False
        self._closed = False
        self._shutdown_task: asyncio.Task[None] | None = None
        self._authenticated_first_party_extension_ids = (
            authenticated_first_party_extension_ids
        )
        self._bundled_skill_binding = BundledSkillDistributionBindingOwner()
        self._credential_boundary = credential_boundary or ProcessCredentialBoundary()
        self.mcp_management = LocalMcpManagementService(
            model_runtime.settings, credential_boundary=self._credential_boundary
        )

    def _canonical_deadline(self) -> float:
        return self._deadlines.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL)

    def _plugin_management(self) -> PluginManagementService:
        return PluginManagementService(credential_boundary=self._credential_boundary)

    async def inspect_user_plugins(self) -> PluginInspectionResult:
        """Inspect only the process-wide user Plugin store."""

        request = InspectLocalPluginsRequest(self._canonical_deadline())
        return await _shielded_plugin_filesystem_call(
            self._plugin_management().inspect_local_plugins,
            request=request,
        )

    async def install_user_plugin(
        self,
        source_path: Path,
        *,
        source_format="native",
        import_classifications=(),
        import_public_values=(),
    ) -> PluginInstallOutcome:
        """Install one local package into the user Plugin store, initially disabled."""

        request = InstallLocalPluginRequest(
            source_path=source_path,
            scope=PluginScopeKind.USER,
            deadline_monotonic=self._canonical_deadline(),
            source_format=source_format,
            import_classifications=import_classifications,
            import_public_values=import_public_values,
        )
        return await self._plugin_management().install_local_plugin(
            request,
            connections=self.mcp_management,
        )

    async def set_user_plugin_enabled(
        self,
        *,
        plugin_id: str,
        package_install_id: str,
        enabled: bool,
        connection_review,
    ) -> PluginEnablementOutcome:
        """Apply one exact reviewed user Plugin enablement cut."""

        request = SetLocalPluginEnabledRequest(
            scope=PluginScopeKind.USER,
            plugin_id=plugin_id,
            enabled=enabled,
            expected_current_package_install_id=package_install_id,
            connection_review=connection_review,
            deadline_monotonic=self._canonical_deadline(),
            external_process_acceptance=(
                ExternalProcessAcceptance.ACCEPTED if enabled else None
            ),
        )
        return await _shielded_plugin_filesystem_call(
            self._plugin_management().set_local_plugin_enabled,
            request=request,
        )

    async def remove_user_plugin(
        self, plugin_id: str, package_install_id: str
    ) -> PluginRemovalOutcome:
        """Remove one user Plugin state reference."""

        request = RemoveLocalPluginRequest(
            scope=PluginScopeKind.USER,
            plugin_id=plugin_id,
            deadline_monotonic=self._canonical_deadline(),
            expected_current_package_install_id=package_install_id,
        )
        return await self._plugin_management().remove_local_plugin(
            request,
            connections=self.mcp_management,
        )

    @classmethod
    def production(
        cls,
        *,
        model_runtime: ModelRuntime,
        authenticated_first_party_extension_ids: frozenset[str] = frozenset(),
        watchdog_policy: KernelExecutionWatchdogPolicy | None = None,
        credential_boundary: ProcessCredentialBoundary | None = None,
    ) -> "KernelHostCore":
        return cls(
            model_runtime=model_runtime,
            authenticated_first_party_extension_ids=(
                authenticated_first_party_extension_ids
            ),
            watchdog_policy=watchdog_policy,
            credential_boundary=credential_boundary,
        )

    async def _ensure_resources(self) -> ConversationKernelRepository:
        if self._repository is None:
            try:
                postgres = self._model_runtime.settings.read().postgres
            except Exception as exc:
                raise KernelCompositionUnavailable(
                    "local PostgreSQL settings are unavailable"
                ) from exc
            if postgres is None:
                raise KernelCompositionUnavailable("PostgreSQL is not configured")
            self._event_loop = asyncio.get_running_loop()
            self._access = await process_postgres_schema_verification_service().acquire(
                postgres.runtime_dsn,
                deadline_monotonic=self._canonical_deadline(),
            )
            try:
                await asyncio.to_thread(
                    require_stage2_runtime_privilege_boundary,
                    self._access.connection_provider,
                    deadline_monotonic=self._canonical_deadline(),
                )
            except BaseException:
                self._access.release()
                self._access = None
                raise
            self._repository = ConversationKernelRepository(
                self._access.connection_provider,
                post_commit_tap=self._route_committed_events_from_thread,
            )
            self._blob_store = PostgresCanonicalBlobStore(
                self._access.connection_provider
            )
            self._blob_gc_io = KernelSessionIO(maximum_concurrency=1)
            self._blob_gc_task = asyncio.create_task(
                self._blob_gc_loop(), name="kernel-blob-orphan-gc"
            )
        return self._repository

    async def fork_conversation(
        self,
        *,
        source_session_id: str,
        anchor_entry_id: str,
        child_session_id: str,
        memory_domain_id: str,
    ):
        """Canonical copy only. Opening the committed child uses ordinary resume."""
        settlement = await self._admit_session_open()
        try:
            repository = await self._ensure_resources()
            deadline = self._canonical_deadline()
            worker = asyncio.create_task(
                asyncio.to_thread(
                    repository.fork_conversation,
                    source_session_id=source_session_id,
                    anchor_entry_id=anchor_entry_id,
                    child_session_id=child_session_id,
                    memory_domain_id=memory_domain_id,
                    deadline_monotonic=deadline,
                )
            )
            _, cancelled, _ = await _join_task_beyond_logical_deadline(
                worker, deadline_monotonic=deadline
            )
            if cancelled is not None:
                raise cancelled
            return worker.result()
        finally:
            await self._settle_session_open(settlement)

    async def open_session(
        self,
        workspace_input: HostWorkspaceInput,
        *,
        permission_policy: EffectivePermissionPolicy | None = None,
        system_prompt: str | None = None,
        active_skill_names: frozenset[str] = frozenset(),
    ) -> KernelHostSession:
        return await self._open(
            workspace_input,
            session_id=f"session:{uuid4().hex}",
            permission_policy=permission_policy,
            system_prompt=system_prompt,
            active_skill_names=active_skill_names,
            session_start_source="startup",
        )

    async def resume_session(
        self,
        session_id: str,
        *,
        workspace_input: HostWorkspaceInput,
        permission_policy: EffectivePermissionPolicy | None = None,
        system_prompt: str | None = None,
        active_skill_names: frozenset[str] = frozenset(),
    ) -> KernelHostSession:
        return await self._open(
            workspace_input,
            session_id=session_id,
            permission_policy=permission_policy,
            system_prompt=system_prompt,
            active_skill_names=active_skill_names,
            session_start_source="resume",
        )

    async def resume_most_recent_session(
        self,
        workspace_input: HostWorkspaceInput,
        **kwargs: object,
    ) -> KernelHostSession:
        sessions = await self.list_resumable_sessions(
            workspace_input=workspace_input, limit=1
        )
        if not sessions:
            raise KeyError("no resumable runtime session found")
        return await self.resume_session(
            sessions[0].session_id, workspace_input=workspace_input, **kwargs
        )

    async def _open(
        self,
        workspace_input: HostWorkspaceInput,
        *,
        session_id: str,
        permission_policy: EffectivePermissionPolicy | None,
        system_prompt: str | None,
        active_skill_names: frozenset[str],
        session_start_source: str,
    ) -> KernelHostSession:
        settlement = await self._admit_session_open()
        try:
            return await self._open_admitted(
                workspace_input,
                session_id=session_id,
                permission_policy=permission_policy,
                system_prompt=system_prompt,
                active_skill_names=active_skill_names,
                session_start_source=session_start_source,
            )
        finally:
            await self._settle_session_open(settlement)

    async def _admit_session_open(self) -> asyncio.Future[None]:
        async with self._lock:
            if self._closing:
                raise KernelHostCoreClosing("Kernel Host core is closing")
            settlement = asyncio.get_running_loop().create_future()
            self._open_attempts.add(settlement)
            return settlement

    async def _settle_session_open(self, settlement: asyncio.Future[None]) -> None:
        async with self._lock:
            self._open_attempts.discard(settlement)
            if not settlement.done():
                settlement.set_result(None)

    async def _open_admitted(
        self,
        workspace_input: HostWorkspaceInput,
        *,
        session_id: str,
        permission_policy: EffectivePermissionPolicy | None,
        system_prompt: str | None,
        active_skill_names: frozenset[str],
        session_start_source: str,
    ) -> KernelHostSession:
        workspace = resolve_workspace(workspace_input)
        canonical_root = workspace.workspace_root.resolve(strict=False)
        async with self._lock:
            capability_baseline = self._workspace_capability_revisions.setdefault(
                canonical_root, 0
            )
        deadline = self._deadlines.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL)
        user_home_resolution = resolve_user_home()
        pulsara_home_resolution = resolve_pulsara_home(
            user_home_resolution=user_home_resolution
        )
        if pulsara_home_resolution.path is None:
            raise PulsaraHomeResolutionError(pulsara_home_resolution)
        plugin_store = ManagedPluginStore(
            pulsara_home=pulsara_home_resolution,
            credential_boundary=self._credential_boundary,
        )
        plugin_view_owner = EnabledPluginViewOwner(
            store=plugin_store,
            credential_boundary=self._credential_boundary,
        )
        initial_plugin_view = await _shielded_plugin_filesystem_call(
            plugin_view_owner.observe,
            workspace_root=(
                workspace.workspace_root
                if workspace.workspace_kind == "project"
                else None
            ),
            deadline_monotonic=deadline,
            cancellation=NeverCancelPluginOperation(),
        )
        try:
            hook_source_provider = LocalHookSourceProvider(
                workspace_root=workspace.workspace_root,
                workspace_kind=workspace.workspace_kind,
                workspace_state_key=workspace.workspace_key,
                pulsara_home=pulsara_home_resolution.path,
            )
            initial_local_hook_view = await asyncio.to_thread(
                hook_source_provider.discover,
                deadline_monotonic=deadline,
            )
            initial_hook_view = compose_hook_definition_view(
                local_view=initial_local_hook_view,
                plugin_view=initial_plugin_view,
                trust_store=hook_source_provider.trust_store,
            )
            local_mcp_configs = await asyncio.to_thread(
                self.mcp_management.load_configs,
                workspace_root=workspace.workspace_root,
                trust_workspace_config=workspace.trust_workspace_mcp_config,
                approved_workspace_server_identities=(
                    workspace_mcp_server_approvals(workspace.workspace_root)
                ),
            )
            mcp_normalization = normalize_plugin_mcp_configs(
                existing_configs=local_mcp_configs,
                view=initial_plugin_view,
                secret_resolver=self.mcp_management.settings.read().mcp_secret,
                oauth_manager=self.mcp_management.oauth,
                current_state=plugin_view_owner.current_state,
            )
            mcp_configs = mcp_normalization.configs
            repository = await self._ensure_resources()
        except BaseException:
            initial_plugin_view.close()
            raise
        host_id = f"host:{uuid4().hex}"
        io_owner = KernelSessionIO()
        try:
            writer_lease = await io_owner.run(
                repository.acquire_host_writer,
                session_id=session_id,
                workspace_id=workspace.workspace_key,
                workspace_kind=workspace.workspace_kind,
                workspace_root=str(workspace.workspace_root),
                workspace_label=workspace.display_label,
                memory_domain_id=workspace.memory_domain.memory_domain_id,
                writer_owner_id=host_id,
                lease_seconds=self._deadlines.policy.writer_lease_seconds,
                deadline_monotonic=deadline,
            )
            session = KernelHostSession(
                model_runtime=self._model_runtime,
                mcp_management=self.mcp_management,
                capability_change_notifier=self.mark_capability_change,
                workspace=workspace,
                repository=repository,
                writer_lease=writer_lease,
                io_owner=io_owner,
                session_id=session_id,
                host_session_id=host_id,
                permission_policy=permission_policy or default_permission_policy(),
                system_prompt=system_prompt,
                active_skill_names=active_skill_names,
                authenticated_first_party_extension_ids=(
                    self._authenticated_first_party_extension_ids
                ),
                deadline_factory=self._deadlines,
                session_start_source=session_start_source,
                hook_source_provider=hook_source_provider,
                initial_hook_view=initial_hook_view,
                bundled_skill_binding=self._bundled_skill_binding,
                pulsara_home_resolution=pulsara_home_resolution,
                user_home_resolution=user_home_resolution,
                plugin_view_owner=plugin_view_owner,
                initial_plugin_view=initial_plugin_view,
                credential_boundary=self._credential_boundary,
                local_mcp_configs=local_mcp_configs,
                mcp_configs=mcp_configs,
            )
            await session.start_mcp()
            await self._register_prepared_session(session, capability_baseline)
            return session
        except BaseException:
            if "session" in locals():
                with suppress(BaseException):
                    await session.aclose(deadline_monotonic=deadline)
            else:
                with suppress(BaseException):
                    initial_plugin_view.close()
            await io_owner.aclose(deadline_monotonic=deadline)
            raise

    async def _register_prepared_session(
        self, session: KernelHostSession, capability_baseline: int
    ) -> None:
        """Join source capture with publication without missing an in-flight edit."""
        root = session.workspace.workspace_root.resolve(strict=False)
        async with self._lock:
            if self._closing:
                raise KernelHostCoreClosing(
                    "Kernel Host shutdown won before session registration"
                )
            self._sessions[session.host_session_id] = session
            self._extension_routes[session.session_id] = (
                session.host_session_id,
                session.extensions,
            )
            changed = self._workspace_capability_revisions[root] > capability_baseline
        if changed:
            await session.request_capability_refresh()

    async def memory_management_projects(
        self, *, memory_domain_id, limit=40, cursor=None
    ):
        repository = await self._ensure_resources()
        return await asyncio.to_thread(
            repository.memory_management_projects,
            memory_domain_id=memory_domain_id,
            limit=limit,
            cursor=cursor,
            deadline_monotonic=self._canonical_deadline(),
        )

    async def memory_management_catalog(
        self, *, memory_domain_id, selection, **filters
    ):
        repository = await self._ensure_resources()
        return await asyncio.to_thread(
            repository.memory_management_catalog,
            memory_domain_id=memory_domain_id,
            selection=selection,
            deadline_monotonic=self._canonical_deadline(),
            **filters,
        )

    async def memory_management_detail(
        self,
        *,
        memory_domain_id,
        selection,
        fact_id,
        provenance_workspace_id,
        limit=40,
        cursor=None,
    ):
        repository = await self._ensure_resources()
        return await asyncio.to_thread(
            repository.memory_management_detail,
            memory_domain_id=memory_domain_id,
            selection=selection,
            fact_id=fact_id,
            provenance_workspace_id=provenance_workspace_id,
            limit=limit,
            cursor=cursor,
            deadline_monotonic=self._canonical_deadline(),
        )

    async def memory_deletion_preview(
        self, *, memory_domain_id, selection, fact_id, additional=()
    ):
        repository = await self._ensure_resources()
        return await asyncio.to_thread(
            repository.memory_deletion_preview,
            memory_domain_id=memory_domain_id,
            selection=selection,
            fact_id=fact_id,
            additional=additional,
            deadline_monotonic=self._canonical_deadline(),
        )

    async def execute_memory_deletion(
        self, *, memory_domain_id, selection, fact_id, additional, expected_records
    ):
        repository = await self._ensure_resources()
        # Join physical execution before the HTTP owner may close its confirmation file.
        task = asyncio.create_task(
            asyncio.to_thread(
                repository.execute_memory_deletion,
                memory_domain_id=memory_domain_id,
                selection=selection,
                fact_id=fact_id,
                additional=additional,
                expected_records=expected_records,
                deadline_monotonic=self._canonical_deadline(),
            )
        )
        cancelled = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancelled = exc
        result = task.result()
        if cancelled is not None:
            raise cancelled
        return result

    async def mark_capability_change(self, workspace_root: Path | None = None) -> int:
        canonical_root = (
            workspace_root.resolve(strict=False) if workspace_root is not None else None
        )
        async with self._lock:
            roots = (
                {canonical_root}
                if canonical_root is not None
                else set(self._workspace_capability_revisions)
            )
            for root in roots:
                self._workspace_capability_revisions[root] = (
                    self._workspace_capability_revisions.get(root, 0) + 1
                )
            sessions = tuple(
                session
                for session in self._sessions.values()
                if canonical_root is None
                or session.workspace.workspace_root.resolve() == canonical_root
            )
        marked = 0
        for session in sessions:
            try:
                await session.request_capability_refresh()
                marked += 1
            except RuntimeError:
                async with self._lock:
                    if self._sessions.get(session.host_session_id) is session:
                        raise
        return marked

    async def list_resumable_sessions(
        self,
        *,
        workspace_input: HostWorkspaceInput,
        include_closed: bool = False,
        limit: int = 20,
    ) -> list[KernelSessionSummary]:
        if not 1 <= limit <= 100:
            raise ValueError("session list limit is out of bounds")
        repository = await self._ensure_resources()
        workspace = resolve_workspace(workspace_input)
        rows = await asyncio.to_thread(
            _list_resumable_session_rows,
            repository,
            workspace.workspace_key,
            workspace.memory_domain.memory_domain_id,
            include_closed,
            limit,
            self._canonical_deadline(),
        )
        return [_kernel_session_summary(row) for row in rows]

    async def list_resumable_sessions_across_workspaces(
        self,
        *,
        memory_domain_id: str = "u_local",
        include_closed: bool = False,
    ) -> list[KernelSessionSummary]:
        """Cold-list every resumable Session in one local memory domain."""

        repository = await self._ensure_resources()
        rows = await asyncio.to_thread(
            _list_resumable_session_rows_across_workspaces,
            repository,
            memory_domain_id,
            include_closed,
            self._canonical_deadline(),
        )
        return [_kernel_session_summary(row) for row in rows]

    async def read_resumable_session(
        self,
        session_id: str,
        *,
        memory_domain_id: str = "u_local",
        include_closed: bool = False,
    ) -> KernelSessionSummary | None:
        """Cold-read one Session together with its exact workspace input."""

        if not session_id:
            raise ValueError("session_id is required")
        repository = await self._ensure_resources()
        row = await asyncio.to_thread(
            _read_resumable_session_row,
            repository,
            session_id,
            memory_domain_id,
            include_closed,
            self._canonical_deadline(),
        )
        return None if row is None else _kernel_session_summary(row)

    async def read_subagent_task_page(
        self,
        *,
        session_id: str,
        maximum_items: int,
        after_accepted_at: datetime | None = None,
        after_task_id: str | None = None,
        batch_id: str | None = None,
    ) -> tuple[
        tuple[Mapping[str, object], ...],
        tuple[Mapping[str, object], ...],
    ]:
        """Cold-read one durable, session-scoped task page plus its edges."""

        if not session_id:
            raise ValueError("session_id is required")
        if not 1 <= maximum_items <= 50:
            raise ValueError("subagent task page bound is invalid")
        repository = await self._ensure_resources()
        deadline = self._canonical_deadline()
        rows = await asyncio.to_thread(
            repository.list_subagent_tasks,
            session_id=session_id,
            maximum_items=maximum_items,
            after_accepted_at=after_accepted_at,
            after_task_id=after_task_id,
            batch_id=batch_id,
            include_lookahead=True,
            deadline_monotonic=deadline,
        )
        task_ids = tuple(str(row["id"]) for row in rows[:maximum_items])
        dependencies: list[Mapping[str, object]] = []
        for start in range(0, len(task_ids), 32):
            dependencies.extend(
                await asyncio.to_thread(
                    repository.read_subagent_dependencies,
                    session_id=session_id,
                    task_ids=task_ids[start : start + 32],
                    deadline_monotonic=deadline,
                )
            )
        return rows, tuple(dependencies)

    async def read_subagent_task_group_page(
        self,
        *,
        session_id: str,
        maximum_items: int,
        after_first_accepted_at: datetime | None = None,
        after_batch_id: str | None = None,
    ) -> tuple[Mapping[str, object], ...]:
        """Cold-read real batch aggregates without activating a Host."""

        if not session_id:
            raise ValueError("session_id is required")
        if not 1 <= maximum_items <= 50:
            raise ValueError("subagent group page bound is invalid")
        repository = await self._ensure_resources()
        return await asyncio.to_thread(
            repository.list_subagent_task_groups,
            session_id=session_id,
            maximum_items=maximum_items,
            after_first_accepted_at=after_first_accepted_at,
            after_batch_id=after_batch_id,
            include_lookahead=True,
            deadline_monotonic=self._canonical_deadline(),
        )

    async def read_subagent_task_activity_page(
        self,
        *,
        session_id: str,
        task_id: str,
        maximum_items: int,
        after_entry_sequence: int = 0,
    ):
        """Cold-read exact canonical activity identities for one task."""

        if not session_id or not task_id:
            raise ValueError("session and task identity are required")
        repository = await self._ensure_resources()
        return await asyncio.to_thread(
            repository.list_subagent_task_activities,
            session_id=session_id,
            task_id=task_id,
            maximum_items=maximum_items,
            after_entry_sequence=after_entry_sequence,
            deadline_monotonic=self._canonical_deadline(),
        )

    async def close_session(
        self, host_session_id: str, *, close_conversation: bool
    ) -> None:
        async with self._lock:
            session = self._sessions.get(host_session_id)
            attempt = self._close_attempts.get(host_session_id)
            if attempt is None:
                if session is None:
                    if close_conversation:
                        raise HostSessionCloseDecisionFrozen(
                            "canonical close cannot be added after the Host session "
                            "owner was retired"
                        )
                    return
                if close_conversation:
                    session.request_close_conversation()
                deadline = self._deadlines.deadline(
                    KernelWatchdogOwner.HOST_SESSION_CLOSE
                )
                task = asyncio.create_task(
                    self._close_session_owner(
                        host_session_id=host_session_id,
                        session=session,
                        deadline_monotonic=deadline,
                    ),
                    name=f"kernel-host-close:{host_session_id}",
                )
                attempt = HostSessionCloseAttempt(
                    host_session_id=host_session_id,
                    session=session,
                    deadline_monotonic=deadline,
                    close_conversation_requested=close_conversation,
                    task=task,
                )
                self._close_attempts[host_session_id] = attempt
            else:
                attempt.merge_close_conversation(close_conversation)
        # The Host owns the close operation.  Request/gateway cancellation
        # detaches only this waiter; later callers and shutdown join the exact
        # same physical close task.
        await asyncio.shield(attempt.task)

    async def _close_session_owner(
        self,
        *,
        host_session_id: str,
        session: KernelHostSession,
        deadline_monotonic: float,
    ) -> None:
        try:
            await session.aclose(
                close_conversation=False,
                deadline_monotonic=deadline_monotonic,
                freeze_close_conversation_decision=(
                    lambda: self._freeze_close_conversation_decision(
                        host_session_id=host_session_id,
                        session=session,
                    )
                ),
            )
        except BaseException:
            async with self._lock:
                attempt = self._close_attempts.get(host_session_id)
                if attempt is not None:
                    attempt.state = HostSessionCloseState.CLOSE_FAILED_QUARANTINED
            raise
        async with self._lock:
            attempt = self._close_attempts.get(host_session_id)
            if attempt is not None:
                attempt.state = HostSessionCloseState.CLOSED
            if self._sessions.get(host_session_id) is session:
                self._sessions.pop(host_session_id, None)
            route = self._extension_routes.get(session.session_id)
            if route is not None and route[0] == host_session_id:
                self._extension_routes.pop(session.session_id, None)
            # Successful close has no future join/recovery work.  Retaining the
            # attempt would retain its completed task and the entire session
            # object graph for the lifetime of KernelHostCore.  Failed attempts
            # remain quarantined above; successful attempts retire immediately.
            if self._close_attempts.get(host_session_id) is attempt:
                self._close_attempts.pop(host_session_id, None)

    async def _freeze_close_conversation_decision(
        self,
        *,
        host_session_id: str,
        session: KernelHostSession,
    ) -> bool:
        """Linearize the last point at which canonical close can be upgraded."""

        async with self._lock:
            attempt = self._close_attempts.get(host_session_id)
            if attempt is None or attempt.session is not session:
                raise RuntimeError("Host close decision owner is absent")
            return attempt.freeze_close_conversation()

    async def shutdown(self) -> None:
        async with self._lock:
            task = self._shutdown_task
            if task is None:
                self._closing = True
                open_settlements = tuple(self._open_attempts)
                task = asyncio.create_task(
                    self._shutdown_owner(open_settlements),
                    name="kernel-host-core-shutdown",
                )
                self._shutdown_task = task
        waiter_cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if task.done():
                    break
                waiter_cancellation = waiter_cancellation or exc
                continue
            except BaseException:
                break
        if task.cancelled():
            raise asyncio.CancelledError
        task.result()
        if waiter_cancellation is not None:
            raise waiter_cancellation

    async def _shutdown_owner(
        self, open_settlements: tuple[asyncio.Future[None], ...]
    ) -> None:
        if open_settlements:
            await asyncio.gather(*open_settlements)
        await self.mcp_management.aclose()
        async with self._lock:
            session_ids = tuple(self._sessions)
        for host_session_id in session_ids:
            await self.close_session(host_session_id, close_conversation=False)
        async with self._lock:
            self._extension_routes.clear()
        blob_close_deadline = self._deadlines.deadline(
            KernelWatchdogOwner.BLOB_GC_CLOSE
        )
        blob_close_deadline_expired = False
        blob_close_cancellation: asyncio.CancelledError | None = None
        blob_close_error: BaseException | None = None
        if self._blob_gc_task is not None:
            self._blob_gc_task.cancel()
            (
                task_deadline_expired,
                task_cancellation,
                task_error,
            ) = await _join_task_beyond_logical_deadline(
                self._blob_gc_task,
                deadline_monotonic=blob_close_deadline,
            )
            blob_close_deadline_expired |= task_deadline_expired
            blob_close_cancellation = task_cancellation
            blob_close_error = task_error
            self._blob_gc_task = None
        if self._blob_gc_io is not None:
            io_close_task = asyncio.create_task(
                self._blob_gc_io.aclose(deadline_monotonic=blob_close_deadline),
                name="kernel-blob-orphan-gc-io-close",
            )
            (
                io_deadline_expired,
                io_cancellation,
                io_error,
            ) = await _join_task_beyond_logical_deadline(
                io_close_task,
                deadline_monotonic=blob_close_deadline,
            )
            blob_close_deadline_expired |= io_deadline_expired
            blob_close_cancellation = blob_close_cancellation or io_cancellation
            blob_close_error = blob_close_error or io_error
            self._blob_gc_io = None
        self._blob_store = None
        if self._access is not None:
            self._access.release()
            self._access = None
            self._repository = None
            self._event_loop = None
        await self._bundled_skill_binding.aclose()
        async with self._lock:
            self._closed = True
        if blob_close_error is not None:
            raise blob_close_error
        if blob_close_cancellation is not None:
            raise blob_close_cancellation
        if blob_close_deadline_expired:
            raise TimeoutError("blob GC physical owner exited after close deadline")

    def _route_committed_events_from_thread(
        self, events: tuple[StoredCommittedEvent, ...]
    ) -> None:
        """Cross the physical transaction boundary without waiting on a hook."""

        loop = self._event_loop
        if loop is None or loop.is_closed() or not events:
            return
        try:
            loop.call_soon_threadsafe(self._deliver_committed_events, events)
        except RuntimeError:
            # A process-local observer cannot keep a closing event loop alive.
            return

    def _deliver_committed_events(
        self, events: tuple[StoredCommittedEvent, ...]
    ) -> None:
        for event in events:
            route = self._extension_routes.get(event.session_id)
            if route is None:
                continue
            route[1].offer_post_commit_nowait(
                PostCommitHookOffer(
                    event_type=event.event_type,
                    session_id=event.session_id,
                    turn_id=event.turn_id,
                    subject_id=event.subject.subject_id,
                    event_sequence=event.event_sequence,
                    public_payload={
                        "subject_slot": event.subject.slot.value,
                        "accepted_at": event.accepted_at.isoformat(),
                        "occurred_at": event.occurred_at.isoformat(),
                        "actor_kind": event.actor_kind,
                        "sensitivity_class": event.sensitivity_class,
                        "projection_profile": event.projection_profile,
                        "payload": dict(event.payload),
                    },
                )
            )

    async def _blob_gc_loop(self) -> None:
        store = self._blob_store
        io_owner = self._blob_gc_io
        assert store is not None and io_owner is not None
        while True:
            try:
                await io_owner.run(
                    store.delete_orphans,
                    grace_seconds=STAGE2_LIMITS.blob_orphan_grace_seconds,
                    maximum_items=STAGE2_LIMITS.blob_gc_batch_hard_items,
                    deadline_monotonic=(
                        monotonic() + STAGE2_LIMITS.foreground_io_timeout_ms / 1_000
                    ),
                )
            except asyncio.CancelledError:
                raise
            except BaseException:
                # Orphan collection is disposable maintenance.  The next
                # bounded interval retries; product writes never wait for it.
                pass
            await asyncio.sleep(STAGE2_LIMITS.blob_gc_interval_ms / 1_000)


async def _acquire_lock_before_deadline(
    lock: asyncio.Lock,
    deadline_monotonic: float,
    message: str,
) -> None:
    remaining = deadline_monotonic - monotonic()
    if remaining <= 0:
        raise TimeoutError(message)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=remaining)
    except TimeoutError:
        raise TimeoutError(message) from None


def _raise_if_deadline_expired(deadline_monotonic: float, message: str) -> None:
    if monotonic() >= deadline_monotonic:
        raise TimeoutError(message)


def _plan_workflow_command_outcome(
    accepted: AcceptedPlanWorkflowCommand,
) -> KernelCommandOutcome:
    code = {
        "ACTIVE": "PLAN_ENTERED",
        "CANCELLED": "PLAN_CANCELLED",
        "FORCE_EXITED": "PLAN_FORCE_EXITED",
    }[accepted.workflow_status.value]
    return KernelCommandOutcome(
        command_id=accepted.command_id,
        status="SUCCEEDED",
        target_id=accepted.workflow_id,
        public_code=code,
        public_message="Plan workflow transition accepted.",
        plan_workflow_status=accepted.workflow_status.value,
        resume_permission_mode=accepted.resume_permission_mode,
        handoff_created_at_commit=accepted.handoff_created_at_commit,
        plan_workflow_revision=accepted.workflow_revision,
    )


__all__ = [
    "KernelCapabilityCatalogInspection",
    "KernelCommandOutcome",
    "KernelCompositionUnavailable",
    "KernelHostCore",
    "KernelHostSession",
    "KernelMcpToolCatalogItem",
    "KernelSessionSummary",
    "HostSessionCloseDecisionFrozen",
]
