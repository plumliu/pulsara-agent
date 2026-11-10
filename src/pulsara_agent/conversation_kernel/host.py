"""Production Host composition for the Stage 2 conversation kernel.

This composition deliberately has only canonical conversation, process-local
execution, selective journal, and exact-four background-job owners. Resume
acquires a new writer generation and rehydrates canonical rows only.
"""

# Reexport the existing Host import surface while implementation lives in siblings.
# ruff: noqa: F401

from __future__ import annotations

from pulsara_agent.conversation_kernel.repository_errors import UnpublishedIdentityCollision

from pulsara_agent.identity import derive_id, random_id, is_valid_id

from pulsara_agent.scheduling.contracts import ScheduledAdmission

from pulsara_agent.conversation_kernel.annotations import AnnotationSourceInvalid

import asyncio
import logging
import json
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from functools import wraps
from pathlib import Path
from time import monotonic
from typing import Awaitable, Callable, Literal, Mapping

from psycopg import IsolationLevel
from psycopg import InterfaceError, OperationalError
from psycopg.rows import dict_row
from pulsara_agent.conversation_kernel.session_deletion import (
    KernelSessionRetirement, SessionDeleteRejected,
)

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
    CompactionWriteReservation,
    HostCompactionRuntimeOwner,
)
from pulsara_agent.conversation_kernel.cancellation import (
    ActiveTurnCancellationIntent,
    ForegroundCancellationCause,
)
from pulsara_agent.conversation_kernel.activation import (
    require_stage2_runtime_privilege_boundary,
)
from pulsara_agent.conversation_kernel.blob import (
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
from pulsara_agent.conversation_kernel.request_execution import (
    RequestExecution,
    RequestExecutionStatus,
)
from pulsara_agent.conversation_kernel.contracts import (
    ConversationScopeKind,
    PromptDeliveryMode,
    StoredCommittedEvent,
    WriterLease,
    canonical_digest,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.history_maintenance import HistoryProjectionMaintainer
from pulsara_agent.conversation_kernel.vocabulary import SubjectSlot, CommittedEventType
from pulsara_agent.conversation_kernel.queued_prompt_actions import (
    QueuedPromptAction,
    QueuedPromptActionRejected,
)
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    DEFAULT_KERNEL_WATCHDOG_POLICY,
    KernelExecutionDeadlineFactory,
    KernelExecutionWatchdogPolicy,
    KernelWatchdogOwner,
)
from pulsara_agent.conversation_kernel.input_continuity import (
    ProcessLocalProviderInputInstallPermit,
    HostProviderInputContinuityOwner,
    _issue_root_bootstrap_lease_source,
)
from pulsara_agent.conversation_kernel.assistant_settlement import (
    AssistantMessageSettlementOwner,
)
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
from pulsara_agent.conversation_kernel.memory.embedding_maintainer import (
    MemoryEmbeddingMaintainer,
)
from pulsara_agent.memory.scope import freeze_memory_read_context_binding
from pulsara_agent.conversation_kernel.query import (
    CanonicalConversationQuery,
    CanonicalProviderCallUsagePage,
)
from pulsara_agent.conversation_kernel.repository import (
    AcceptedPlanResolution,
    AcceptedPlanToolBatch,
    AcceptedPlanWorkflowCommand,
    ConversationKernelConflict,
    ConversationKernelRepository,
    PromptIngressRejected,
    PlanContinuationDisposition,
    PlanQuestionAnswer,
    PlanContinuationInspection,
    PreparedPlanToolBatch,
    PlanToolControlKind,
    StaleHostWriter,
    plan_draft_review_semantic_candidate,
    plan_exit_semantic_fingerprint,
    plan_question_resolution_semantic_fingerprint,
)
from pulsara_agent.conversation_kernel.steer import (
    PreparedQueuedRootTurnAdmission,
    PreparedPromptIngressCommand,
    PreparedCellOutputIngress,
    PromptIngressConfirmationKind,
    PromptIngressWriteRejection,
    PreparedRootTurnIdentity,
    QueuedRootTurnAdmissionConfirmation,
    QueuedRootTurnAdmissionConfirmationKind,
    StartedTurnInputGroup,
    build_direct_root_turn_identity,
    build_prompt_ingress_command,
    build_queued_root_turn_identity,
)
from pulsara_agent.conversation_kernel.image_validation import (
    HostPromptImageValidator,
)
from pulsara_agent.conversation_kernel.prompt_content import (
    FrozenCanonicalPrompt,
    freeze_canonical_prompt,
)
from pulsara_agent.conversation_kernel.prompt_storage import (
    PostgresCanonicalImageReferenceReadPort,
)

from pulsara_agent.conversation_kernel.runner import (
    ConversationKernelRunner,
    KernelRunResult,
)
from pulsara_agent.conversation_kernel.subagent import AgentSessionRuntime
from pulsara_agent.conversation_kernel.user_control import (
    CONTROL_ADMISSION_WINDOW_MS,
    CONTROL_RESULT_RETENTION_MS,
    ControlQueryStatus,
    ProcessControlResult,
    RootControlResult,
    CodeModeControlResult,
    UserControlExecution,
    UserControlOperation,
    UserControlOutcome,
    UserControlRequest,
    UserControlTarget,
    UserControlTargetKind,
)

from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.conversation_kernel.current_work_runtime import (
    FrozenCurrentWorkCloseProjection,
    FrozenCurrentWorkSnapshot,
    PreparedRootWorkActivation,
    build_root_activation,
)
from pulsara_agent.conversation_kernel.tool_outputs import (
    PostgresToolOutputReadPort,
)
from pulsara_agent.conversation_kernel.tool_policy import (
    DefaultToolDispatchAuthorizationPolicy,
)
from pulsara_agent.conversation_kernel.workspace import WorkspaceExecutionGate
from pulsara_agent.conversation_kernel.root_status import LATEST_ROOT_SUMMARY_SQL
from pulsara_agent.workspace_identity import (
    HostWorkspaceInput,
    ResolvedWorkspace,
    resolve_workspace,
    observe_workspace,
    require_workspace,
    WorkspaceUnavailable,
)
from pulsara_agent.llm.model_connections import (
    ModelCallBinding,
    model_call_binding_from_dict,
    model_call_binding_to_dict,
)
from pulsara_agent.llm.input import (
    LLMTextPart,
    PromptContent,
    prompt_text_projection,
)
from pulsara_agent.llm.model_target import FrozenModelResolutionSnapshot
from pulsara_agent.llm.runtime import ModelRuntime, ModelRuntimeUnavailable
from pulsara_agent.llm.normalized_transport import ProviderNormalizedTerminalKind
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.model_input.contracts import (
    ModelInputCompileFailureKind,
    ModelInputScopeKind,
    StructuredModelInputCompileError,
)
from pulsara_agent.tool_permission import (
    EffectivePermissionPolicy,
    default_permission_policy,
    mode_for_policy,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot
from pulsara_agent.primitives.plan_workflow import PlanDraftDecision, PlanHandoffKind
from pulsara_agent.ports.system_prompt import DEFAULT_SYSTEM_PROMPT, DEFAULT_SUBAGENT_SYSTEM_PROMPT
from pulsara_agent.ports.terminal_observation import CellOutputContent
from pulsara_agent.terminal_process.models import TerminalProcessInfo
from pulsara_agent.mcp_config import McpServerConfig
from pulsara_agent.capability.mcp_management import LocalMcpManagementService
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
from pulsara_agent.hooks.presentation import hook_snapshot_model_public
from pulsara_agent.public_projection import public_record
from pulsara_agent.plugins.public_projection import project_hook_public
from pulsara_agent.capability.local_skill_management import LocalSkillManagementService
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


from .host_contracts import HostSessionCloseState, KernelCapabilityCatalogInspection, KernelCommandOutcome, KernelCompositionUnavailable, KernelControlQueryResult, KernelHostCoreClosing, KernelMcpToolCatalogItem, KernelPromptDelivery, KernelSessionSettlementUnconfirmed, KernelSessionSummary, PromptBlockedByHook, RootChainPhase, SessionControlRejected, _IngressHookApiVariant, _IngressHookReservationKey, _PendingRootSuccessorHandoff, _ProcessControlObservation, _PromptAdmissionAttempt, _UserControlAttempt
from .host_tasks import _acquire_lock_before_deadline, _core_database_operation, _join_close_task, _join_task_beyond_logical_deadline, _raise_if_deadline_expired, _restore_workspace_directory, _shielded_plugin_filesystem_call
from .host_management import (
    _kernel_session_summary,
    _list_resumable_session_rows,
    _list_resumable_session_rows_across_workspaces,
    _read_resumable_session_row,
)
from .host_lifecycle import (
    _is_input_resource_exhaustion,
)
from .host_plans import (
    _plan_workflow_command_outcome,
)
from .host_prompts import _HostPromptOperations
from .host_plans import _HostPlanOperations
from .host_controls import _HostControlOperations
from .host_lifecycle import _HostSessionLifecycleOperations
from .host_management import _HostCoreManagementOperations


_KERNEL_RUNTIME_REOPEN_QUIESCENCE_SEAL = object()

@dataclass(frozen=True, slots=True, init=False)
class KernelRuntimeReopenQuiescence:
    session_id: str
    host_session_id: str
    writer_generation: int
    operation_nonce: str
    _owner: "KernelHostSession"
    _consumed: bool

    def __init__(
        self,
        *,
        session_id: str,
        host_session_id: str,
        writer_generation: int,
        operation_nonce: str,
        owner: "KernelHostSession",
        _seal: object,
    ) -> None:
        if (
            _seal is not _KERNEL_RUNTIME_REOPEN_QUIESCENCE_SEAL
            or not session_id
            or not host_session_id
            or writer_generation < 1
            or not operation_nonce
        ):
            raise TypeError("runtime-reopen quiescence is Host-issued")
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "host_session_id", host_session_id)
        object.__setattr__(self, "writer_generation", writer_generation)
        object.__setattr__(self, "operation_nonce", operation_nonce)
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_consumed", False)

    def _consume(self, owner: "KernelHostSession") -> None:
        if self._owner is not owner or self._consumed:
            raise RuntimeError("runtime-reopen quiescence is stale")
        object.__setattr__(self, "_consumed", True)

@dataclass(slots=True)
class KernelSessionOpenAttempt:
    session_ids: frozenset[str]
    task: asyncio.Task[KernelHostSession] | asyncio.Task[object] | None = None
    scheduled: bool = False
    configuration: (
        tuple[HostWorkspaceInput, EffectivePermissionPolicy, str | None, frozenset[str]]
        | None
    ) = None
    candidate_session: KernelHostSession | None = None
    io_owner: KernelSessionIO | None = None

@dataclass(slots=True)
class HostSessionCloseAttempt:
    """Unique process-local close owner installed by ``KernelHostCore``."""

    host_session_id: str
    session: "KernelHostSession"
    deadline_monotonic: float
    task: asyncio.Task[None]
    state: HostSessionCloseState = HostSessionCloseState.TASK_INSTALLED


@dataclass(slots=True)
class _CoreResourceInitialization:
    """Candidates belong to this one operation until publication or cleanup."""
    deadline_monotonic: float
    task: asyncio.Task[ConversationKernelRepository] | None = None
    access: VerifiedPostgresAccessLease | None = None
    history: HistoryProjectionMaintainer | None = None
    io: KernelSessionIO | None = None
    cleanup_complete: bool = False


class KernelHostSession(
    _HostPromptOperations,
    _HostPlanOperations,
    _HostControlOperations,
    _HostSessionLifecycleOperations,
):
    def __init__(
        self,
        *,
        model_runtime: ModelRuntime,
        agent_runtime: AgentSessionRuntime,
        root_session_id: str | None,
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
        image_validator: HostPromptImageValidator,
        mcp_management: LocalMcpManagementService,
        capability_change_notifier: Callable[[Path | None], Awaitable[int]]
        | None = None,
        local_mcp_configs: tuple[McpServerConfig, ...] = (),
        mcp_configs: tuple[McpServerConfig, ...] = (),
        scheduled_tasks,
        media_cache=None,
        media_asr_runtime=None,
        control_monotonic: Callable[[], float] = monotonic,
    ) -> None:
        self._model_runtime = model_runtime
        self._credential_boundary = credential_boundary
        self._agent_runtime = agent_runtime
        self.root_session_id = root_session_id
        self._session_scope_kind = ModelInputScopeKind.SUBAGENT if root_session_id is not None else ModelInputScopeKind.ROOT
        self._image_validator = image_validator
        self.mcp_management = mcp_management
        self._capability_change_notifier = capability_change_notifier
        self.workspace = workspace
        self.workspace_gate = WorkspaceExecutionGate(workspace.workspace_root)
        self._pending_workspace_recreated: datetime | None = None
        self._pending_process_controls: dict[str, _ProcessControlObservation] = {}
        self.repository = repository
        self.runtime_session_id = session_id
        self.session_id = session_id
        self.host_session_id = host_session_id
        from pulsara_agent.conversation_kernel.ephemeral import SessionEphemeralProducts
        self._ephemeral = SessionEphemeralProducts(self)
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
        self._input_continuity = HostProviderInputContinuityOwner(
            root_lease_source=_issue_root_bootstrap_lease_source(writer_lease),
            session_kind=self._session_scope_kind,
        )
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
        self._cell_output_wake = asyncio.Event()
        self._hook_diagnostics = LoggingHookDiagnosticAdapter()
        self._scheduled_tasks = scheduled_tasks
        self._capability_refresh_wake = asyncio.Event()
        self._hook_context = HookContextOwner(self._hook_diagnostics)
        self._hook_root_scope = HookDispatchScopeRef(
            self,
            workspace,
            HookScopeKind.SUBAGENT if root_session_id is not None else HookScopeKind.ROOT,
            session_id=session_id,
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

        from pulsara_agent.conversation_kernel.session_content import SessionContentQuery

        self._tools = DirectKernelToolPort(
            workspace_root=workspace.workspace_root,
            host_owner_id=host_session_id,
            shared_terminal_owner=(
                self._agent_runtime.shared_terminal_owner(root_session_id)
                if root_session_id is not None else None
            ),
            session_id=session_id,
            live_bus=self.live_bus,
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
            result_source_reader=PostgresToolOutputReadPort(
                repository.connection_provider,
                session_id=session_id,
                workspace_id=workspace.workspace_key,
            ),
            image_reference_read_port=PostgresCanonicalImageReferenceReadPort(
                repository.connection_provider,
                session_id=session_id,
                workspace_id=workspace.workspace_key,
            ),
            scheduled_tasks=scheduled_tasks,
            memory_domain_id=workspace.memory_domain.memory_domain_id,
            session_content_query=SessionContentQuery(repository, session_id=session_id,
                                  memory_domain_id=workspace.memory_domain.memory_domain_id),
            deadline_factory=self._deadlines,
            user_home_resolution=user_home_resolution,
            image_validator=image_validator,
            media_cache=media_cache,
            media_asr_runtime=media_asr_runtime,
            read_media_settings=model_runtime.settings.read,
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
                workspace_kind=workspace.workspace_kind,
                hooks=hook_source_provider,
                hook_review_tools=self._tools.observe_hook_review_tools,
                skills=LocalSkillManagementService(
                    pulsara_home_resolution=pulsara_home_resolution,
                    user_home_resolution=user_home_resolution,
                ),
                deadline=lambda: self._deadlines.deadline(
                    KernelWatchdogOwner.NONTERMINAL_TOOL_INVOCATION
                ),
            )
        )
        from pulsara_agent.conversation_kernel.subagents.collaboration_api import CollaborationToolPort
        self._collaboration = CollaborationToolPort(
            repository=repository,
            io=self._io,
            root_session_id=root_session_id or session_id,
            session_id=session_id,
            runtime=agent_runtime,
            admission_facts=self._agent_admission_facts,
        )
        self._tools.bind_subagent_port(self._collaboration)
        memory_read_binding = freeze_memory_read_context_binding(
            domain=workspace.memory_domain,
            host_workspace_id=workspace.workspace_key,
        )
        retrieval = RetrievalConfig()
        from pulsara_agent.retrieval.decision import DecisionService
        self._decision_service = DecisionService()
        self._memory_tools = KernelMemoryToolPort(
            repository=repository,
            session_id=session_id,
            read_binding=memory_read_binding,
            embedding_config=retrieval.embedding,
            rerank_config=retrieval.rerank,
            feature_config=retrieval.memory,
            io_owner=self._io,
            settings=model_runtime.settings,
            decision_service=self._decision_service,
        )
        self._memory_tools.bind_deadline_factory(self._deadlines)
        self._memory_embedding_maintainer = MemoryEmbeddingMaintainer(
            repository=repository,
            read_binding=memory_read_binding,
            io_owner=self._io,
            deadline_factory=self._deadlines,
            embedding_port=self._memory_tools,
            session_id=session_id,
        )
        self._memory_tools.bind_embedding_maintainer(self._memory_embedding_maintainer)
        self._tools.bind_memory_port(self._memory_tools)
        from pulsara_agent.conversation_kernel.mcp.supervisor import McpHostSupervisor

        self._mcp_supervisor = McpHostSupervisor(
            workspace_root=workspace.workspace_root,
            configs=mcp_configs,
            credential_boundary=credential_boundary,
            configuration_change=self._capability_refresh_wake,
            adopt_configuration=self._adopt_capabilities_for_execution,
        )
        self._tools.bind_mcp_supervisor(self._mcp_supervisor)
        from pulsara_agent.code_mode.host import CodeModeHost
        self._code_mode = CodeModeHost()
        self._tools.bind_code_mode_port(self._code_mode)
        from pulsara_agent.conversation_kernel.decision_tools import KernelDecisionToolPort
        self._decision_tools = KernelDecisionToolPort(
            settings=model_runtime.settings,
            decision_service=self._decision_service,
            deadline_factory=self._deadlines,
            max_value_bytes=self._code_mode.limits.value_bytes,
        )
        self._tools.bind_decision_port(self._decision_tools)
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
        )
        display_timezone = datetime.now().astimezone().tzinfo
        if display_timezone is None:
            raise RuntimeError("Host display timezone is unavailable")
        self._context_sources = KernelContextSourceCollector(
            workspace_kind=workspace.workspace_kind,
            workspace_root=workspace.workspace_root,
            capability_composer=self._capabilities,
            base_system_prompt=(system_prompt if system_prompt is not None else
                DEFAULT_SUBAGENT_SYSTEM_PROMPT if root_session_id is not None else DEFAULT_SYSTEM_PROMPT),
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
            workspace_gate=self.workspace_gate,
            before_provider_preparation=self._adopt_capabilities_for_execution,
            take_user_actions=self.take_user_actions,
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
            root_work_admission_finalizer=self._finalize_root_work_activation,
            compaction_owner=self._compaction,
            root_session_id=self.root_session_id,
            hook_dispatcher=self._hooks,
            hook_context_owner=self._hook_context,
            hook_scope=self._hook_root_scope,
            session_start_source=session_start_source,
            presentation_notice_sink=self._offer_presentation_notice,
        )
        self._active_task: asyncio.Task[KernelRunResult] | None = None
        self._active_turn_id: str | None = None
        self._active_turn_started = False
        self._active_work_id: str | None = None
        self._active_cancellation_intent: ActiveTurnCancellationIntent | None = None
        self._active_command_id: str | None = None
        self._active_root_phase: RootChainPhase | None = None
        self._pending_root_successor: _PendingRootSuccessorHandoff | None = None
        self._external_new_turn_accepting = False
        self._plan_exit_fence = False
        self._external_new_turn_cancellation_intent: ActiveTurnCancellationIntent | None = None
        self._external_new_turn_settled = asyncio.Event()
        self._external_new_turn_settled.set()
        self._command_failures: dict[str, KernelCommandOutcome] = {}
        self._user_control_attempts: dict[str, _UserControlAttempt] = {}
        self._lock = asyncio.Lock()
        self._capability_reload_settlement_lock = asyncio.Lock()
        self._capability_refresh_requested_revision = 0
        self._capability_refresh_applied_revision = 0
        self._capability_refresh_attention: str | None = None
        self._prompt_admission_attempts: dict[str, _PromptAdmissionAttempt] = {}
        self._queued_prompt_hooks: dict[str, tuple[str | None, PendingHookContextReservation | None]] = {}
        self._runtime_reopen_quiescence: KernelRuntimeReopenQuiescence | None = None
        self._compaction_write_reservations: dict[
            ModelInputScopeKind, set[CompactionWriteReservation]
        ] = {}
        self._manual_compaction_command_attempts: dict[
            str, asyncio.Task[CompactionOutcome]
        ] = {}
        self._compaction.bind_host_fence_callbacks(
            install=self._install_compaction_fence,
            remove=self._remove_compaction_fence,
        )
        self._close_async_lock = asyncio.Lock()
        self._closing = False
        self._closed = False
        self._physical_close_complete = False
        self._necessary_settlement_error: BaseException | None = None
        self._writer_renewal_failure: Exception | None = None
        self._queue_wake = asyncio.Event()
        self._renewal_task = asyncio.create_task(
            self._renew_writer(), name=f"kernel-writer-renew:{session_id}"
        )
        self._delivery_task: asyncio.Task[None] | None = None
        self._code_mode.set_output_ready_callback(self._cell_output_wake.set)
        self._cell_output_task: asyncio.Task[None] | None = None

    def activate_execution(self) -> None:
        """Core calls this only after complete assembly and route registration."""
        self._require_open()
        if self._delivery_task is not None:
            raise RuntimeError("Host execution is already active")
        self._delivery_task = asyncio.create_task(
            self._prompt_delivery_loop(),
            name=f"kernel-prompt-delivery:{self.session_id}",
        )
        self._cell_output_task = asyncio.create_task(
            self._cell_output_delivery_loop(),
            name=f"kernel-cell-output-delivery:{self.session_id}",
        )
        if self.root_session_id is None:
            self._memory_embedding_maintainer.start()
        self._queue_wake.set()

    def mcp_readiness_progress(self) -> dict[str, object] | None:
        return self._mcp_supervisor.readiness_progress()

    async def _agent_admission_facts(self, context, requests) -> dict[str, object]:
        if self.root_session_id is not None or context.session_id != self.session_id:
            raise PermissionError("Only ROOT may delegate new Agent work")
        self._require_open()
        return {"root_guard": self._lease.guard,
                "actor_id": self.host_session_id,
                "deadline_monotonic": self._canonical_deadline()}

    async def _install_compaction_fence(
        self,
        scope: CompactionScope,
        trigger: CompactionTrigger,
        attempt_id: str,
        owner_task: asyncio.Task[object],
        admitted_writer: CompactionWriteReservation | None,
    ) -> None:
        """Recapture and install one exact-scope fence under the Host lock."""

        async with self._lock:
            self._require_open()
            key = self._compaction.scope_key(
                scope.scope_kind
            )
            admitted_writers = self._compaction_write_reservations.get(key, set())
            external_recovery_owner = (
                admitted_writer is not None
                and admitted_writers == {admitted_writer}
                and scope.scope_kind is self._session_scope_kind
            )
            if admitted_writer is not None and not external_recovery_owner:
                raise RuntimeError(
                    "compaction admitted-writer reservation is not the exact owner"
                )
            if admitted_writers and not external_recovery_owner:
                raise RuntimeError("compaction target has an admitted writer")
            self._retire_done_active_root_locked()
            active_owner = self._active_task is owner_task
            idle_owner = self._active_task is None
            if not (active_owner or idle_owner or external_recovery_owner):
                raise RuntimeError("compaction ROOT task ownership changed")
            if (
                active_owner
                and not external_recovery_owner
                and self._active_turn_id != scope.turn_id
            ):
                raise RuntimeError("compaction active ROOT target changed")
            if (
                idle_owner
                and not external_recovery_owner
                and (
                    self._external_new_turn_accepting
                    or self._pending_root_successor is not None
                )
            ):
                raise RuntimeError("compaction idle ROOT admission is busy")
            if self._plan_exit_fence:
                raise RuntimeError("compaction conflicts with Plan force exit")
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
            self._cell_output_wake.set()

    def _reserve_compaction_write_locked(
        self,
        *,
        scope_kind: ModelInputScopeKind,
    ) -> CompactionWriteReservation:
        key = self._compaction.scope_key(scope_kind)
        if self._compaction.is_fenced(
            scope_kind=scope_kind,
        ):
            raise RuntimeError("COMPACTION_IN_PROGRESS")
        reservation = CompactionWriteReservation(
            scope_kind=scope_kind,
        )
        self._compaction_write_reservations.setdefault(key, set()).add(reservation)
        return reservation

    async def _release_compaction_write_reservation(
        self, reservation: CompactionWriteReservation
    ) -> None:
        async with self._lock:
            self._release_compaction_write_reservation_locked(reservation)

    def _release_compaction_write_reservation_locked(
        self, reservation: CompactionWriteReservation
    ) -> None:
        key = self._compaction.scope_key(
            reservation.scope_kind,
        )
        owners = self._compaction_write_reservations.get(key)
        if owners is None or reservation not in owners:
            raise RuntimeError("compaction write reservation is not active")
        owners.remove(reservation)
        if not owners:
            self._compaction_write_reservations.pop(key, None)

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
        from pulsara_agent.hooks.presentation import hook_model_enabled

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
                    "enabled": hook_model_enabled(snapshot),
                    "definitions": len(snapshot.definitions),
                }
                for snapshot in view.source_snapshots
            ],
        }
        scrub = HookSecretScrubSet.capture()
        return public_record(result, scrub, fixed=("status",), children={
            "sources": [public_record(source, scrub, fixed=("source", "scan", "trust")) for source in result["sources"]],
        })

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
            predecessor_plugin_view = self._plugin_view
            self._local_mcp_configs = local_mcp_configs
            try:
                result = await self._reload_capabilities_serialized(deadline)
            except BaseException:
                # Only a publication abort still owns the complete predecessor.
                # Published Plugin/Skill and native MCP cuts cannot be rolled back
                # by changing this Host's source tuple.
                if self._plugin_view is predecessor_plugin_view:
                    self._local_mcp_configs = predecessor_local_mcp_configs
                raise
            if result.get("interruption") == "CANCELLED":
                result["local_hooks"] = "PENDING"
                result["status"] = "PARTIAL"
                return result
            try:
                local = await self.reload_hooks(deadline_monotonic=deadline)
                local_sources = [item for item in local["sources"] if item["source"] != HookSourceKind.PLUGIN.value]
                result["local_hook_sources"] = local_sources
                local_complete = all(item["scan"] == "COMPLETE" and
                                     item["trust"] != "UNAVAILABLE" for item in local_sources)
                result["local_hooks"] = "RELOADED" if local_complete else "PARTIAL"
            except asyncio.CancelledError:
                # Prior cuts already settled. The local publisher owns whether
                # its candidate was admitted; keep the exact completed parts.
                result["local_hooks"] = "PARTIAL"
                result["interruption"] = "CANCELLED"
            except Exception:
                result["local_hooks"] = "PARTIAL"
            if result["local_hooks"] != "RELOADED":
                result["status"] = "PARTIAL"
            return result
        finally:
            self._capability_reload_settlement_lock.release()

    async def adopt_capability_management_change(self, *, workspace_root: Path | None):
        pending = 0
        if self._capability_change_notifier is not None:
            pending = await self._capability_change_notifier(workspace_root)
        await self.request_capability_refresh()
        parts = {}
        await self._adopt_capabilities_if_requested(outcome_sink=parts)
        attention = bool(self.capability_refresh_attention)
        return {
            "status": "PARTIAL" if attention else "RELOADED",
            "reloaded_sessions": int(not attention),
            "pending_sessions": max(0, pending - 1),
            "attention_sessions": int(attention),
            "parts": parts,
            "attention": self.capability_refresh_attention,
        }

    async def request_capability_refresh(self) -> int:
        """Mark the next unprepared dispatch; do not wake an otherwise idle turn."""

        async with self._lock:
            self._require_open()
            self._capability_refresh_requested_revision += 1
            self._capability_refresh_wake.set()
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

    async def _adopt_capabilities_for_execution(self) -> bool:
        return await self._adopt_capabilities_if_requested(propagate_failure=True)

    async def _adopt_capabilities_if_requested(
        self, *, outcome_sink: dict[str, object] | None = None,
        propagate_failure: bool = False,
    ) -> bool:
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
            if propagate_failure:
                raise
            attention = "PROJECT_CAPABILITY_ADOPTION_FAILED"
        else:
            if outcome_sink is not None:
                outcome_sink.update(outcome)
            if outcome.get("status") != "RELOADED":
                attention = "PROJECT_CAPABILITY_ADOPTION_INCOMPLETE"
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
            self._plugin_view_owner.observe_for_runtime,
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
            mcp = await _shielded_plugin_filesystem_call(
                normalize_plugin_mcp_configs,
                close_result=lambda value: value.close_plugin_anchors(),
                existing_configs=self._local_mcp_configs,
                view=replacement,
                secret_resolver=self.mcp_management.settings.read().mcp_secret,
                oauth_bind=self.mcp_management.bind_authorization,
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
        interrupted = False
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
                await _join_task_beyond_logical_deadline(reload_task, deadline_monotonic=deadline)
                mcp_status = "PARTIAL"
                interrupted = True
            except TimeoutError:
                # The native owner decides whether its synchronous config cut
                # became FULL.  Join its exact confirmation/cleanup owner, but
                # retain the caller's logical timeout as a partial reload.
                _, waiter_cancellation, _ = await _join_task_beyond_logical_deadline(
                    reload_task, deadline_monotonic=deadline
                )
                interrupted = waiter_cancellation is not None
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
            "status": "RELOADED" if mcp_status == "RELOADED" and replacement.disposition.value == "COMPLETE"
                      and skill_definitions.disposition.value == "COMPLETE" and all(
                          item.disposition.value == "COMPLETE" and item.trust.disposition.value != "UNAVAILABLE"
                          for item in plugin_only_hooks) else "PARTIAL",
            "plugin_view": replacement.disposition.value,
            "skill_producer": skill_definitions.disposition.value,
            "hook_sources": len(
                tuple(
                    item
                    for item in hook_view.source_snapshots
                    if item.provenance.identity.kind is HookSourceKind.PLUGIN
                )
            ),
            "plugin_hook_sources": [hook_snapshot_model_public(item) for item in plugin_only_hooks],
            "mcp": mcp_status,
            "mcp_changed_server_ids": sorted(changed),
            "diagnostics": reload_diagnostics,
            "same_epoch_prefix": "UNCHANGED",
        }
        if interrupted:
            result["interruption"] = "CANCELLED"
        scrub = HookSecretScrubSet.capture()
        return public_record(result, scrub,
            fixed=("status", "plugin_view", "skill_producer", "mcp", "diagnostics", "same_epoch_prefix", "interruption"),
            children={"plugin_hook_sources": [project_hook_public(source, scrub) for source in result["plugin_hook_sources"]]})

    def reconnect_mcp_server(self, server_id: str) -> None:
        """Request a fresh physical generation for future safe-point borrows."""

        self._require_open()
        self._mcp_supervisor.reconnect(server_id)

    def list_mcp_prompts(self, *, offset=0, limit=200, query=""):
        self._require_open()
        if self._session_scope_kind is not ModelInputScopeKind.ROOT:
            raise ValueError("Prompt selection requires the conversation owner")
        return self._mcp_supervisor.list_user_prompts(offset=offset, limit=limit, query=query)

    async def preview_mcp_prompt(self, target, arguments):
        self._require_open()
        if self._session_scope_kind is not ModelInputScopeKind.ROOT:
            raise ValueError("Prompt selection requires the conversation owner")
        return await self._mcp_supervisor.preview_user_prompt(target, arguments)

    def inspect_capability_catalog(self) -> KernelCapabilityCatalogInspection:
        """Freeze one UI-safe view without creating another capability owner."""

        self._require_open()
        mcp = self._tools.inspect_mcp_discovery_catalog()
        skills = self._capabilities.freeze_owner_snapshot(
            conversation_scope_kind=self._session_scope_kind,
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
    def writer_lost(self) -> bool:
        return isinstance(self._writer_renewal_failure, StaleHostWriter)

    @property
    def writer_unavailable(self) -> bool:
        return self._writer_renewal_failure is not None

    @property
    def active_skill_names(self) -> frozenset[str]:
        return self._capabilities.configured_active_skill_names

    def current_work_snapshot(self) -> FrozenCurrentWorkSnapshot:
        return self._tools.current_work_owner.current_work_snapshot()

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
        result = await self._io.run(
            self.repository.update_session_model_call_binding,
            self._lease.guard,
            binding=accepted,
            deadline_monotonic=self._canonical_deadline(),
        )
        self._ephemeral.invalidate()
        self._ephemeral.invalidate_cache_warming()
        return result

    def invalidate_cache_warming(self) -> None:
        self._ephemeral.invalidate_cache_warming()

    def invalidate_input_suggestion(self) -> None:
        self._ephemeral.invalidate(preserve_running=True)

    def read_ephemeral_results(self) -> dict[str, object]:
        return self._ephemeral.read()

    async def read_context_usage(self) -> dict[str, object]:
        """Advisory ROOT input preview; never installs or opens a provider call.

        Reuse the normal wire projection for idle history, including assistant
        replay. During execution only inspect the installed request. New-turn
        memory/hook sources and the unsent draft are not part of this preview.
        """
        async with self._lock:
            self._require_open()
            return await self._read_context_usage_admitted()

    async def _read_context_usage_admitted(self) -> dict[str, object]:
        from pulsara_agent.conversation_kernel.direct_model import KernelModelTargetPreparationRequest
        from pulsara_agent.model_input.continuity import ProviderInputContinuityScope
        from pulsara_agent.primitives.model_call import ModelCallPurpose
        binding = await self.model_call_binding()
        if binding is None:
            return {"state": "unavailable", "connection_id": None}
        scope = ProviderInputContinuityScope(
            self.session_id, self._session_scope_kind
        )
        cohort = self._input_continuity.current_cohort(scope)
        turn_id = self._active_turn_id
        compacting = self._compaction.is_fenced(
            scope_kind=self._session_scope_kind,
        )
        busy = self._active_task is not None or compacting
        if not busy:
            turn_id = await self._io.run(
                self.repository.read_latest_terminal_scope_turn_id,
                self._lease.guard,
                scope_kind=ConversationScopeKind.ROOT,
                deadline_monotonic=self._canonical_deadline(),
            )
        target = self._model.prepare_target(
            KernelModelTargetPreparationRequest(
                session_id=self.session_id,
                turn_id=turn_id or "context-preview:empty",
                model_call_index=1,
                purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
                maximum_input_tokens=self._runner._provider_dispatch._maximum_input_tokens_per_call,
                binding=binding,
            )
        )
        target_fact = target.epoch_call_target.target_bundle.target_fact
        switched = (
            cohort is not None
            and cohort.target_bundle != target.epoch_call_target.target_bundle
        )
        policy = self._compaction.policy
        automatic = policy.enabled and (
            switched
            or (
                policy.automatic_enabled
                and self._compaction.automatic_allowed(
                    scope_kind=self._session_scope_kind,
                )
            )
        )
        payload: dict[str, object] = {
            "state": "compacting" if compacting else "updating" if busy else "empty",
            "connection_id": binding.connection_id.value,
            "model_id": target_fact.model_id,
            "input_tokens": None,
            "input_budget_tokens": target.effective_input_budget_tokens,
            "budget_source": None,
            "model_switch_pending": switched,
            "automatic_compaction_available": automatic,
            "compaction_expected": False,
            "compaction_trigger_tokens": int(
                target.effective_input_budget_tokens
                * policy.auto_trigger_ratio
            ),
        }
        if busy:
            if cohort is None or switched or compacting:
                return payload
            plan = cohort.view.wire_input_plan
            quote = plan.quote
            tokens, source = quote.budget_input_tokens, quote.budget_source
            anchor = self._input_continuity.current_usage_anchor(scope)
            if anchor is not None:
                calibrated = anchor.budget_for(
                    connection_id=binding.connection_id.value,
                    target=target_fact,
                    route_wire_profile_fingerprint=plan.route_wire_profile_fingerprint,
                    materialization=plan.materialization,
                    raw_input_tokens=quote.raw_final_wire_estimated_input_tokens,
                )
                if calibrated is not None:
                    tokens, source = calibrated[0], "reported_input_anchor"
            payload.update(
                state="ready", input_tokens=tokens, budget_source=source,
                input_budget_tokens=quote.effective_input_budget_tokens,
                compaction_trigger_tokens=int(
                    quote.effective_input_budget_tokens * policy.auto_trigger_ratio
                ),
            )
        elif turn_id is not None:
            projection = (
                await self._runner._provider_dispatch.prepare_compaction_source(
                    turn_id=turn_id,
                    model_call_index=1,
                    inherited_memory_use_policy=self._runner._root_memory_use_policy,
                    deadline=self._canonical_deadline(),
                    allow_terminal_compaction=True,
                    prepared_target_override=target,
                    independent_auxiliary_source=(switched or cohort is None),
                    read_only_context_preview=True,
                )
            )
            try:
                quote = projection.wire_quote
                payload.update(
                    state="ready",
                    input_tokens=quote.budget_input_tokens,
                    input_budget_tokens=quote.effective_input_budget_tokens,
                    budget_source=quote.budget_source,
                    compaction_trigger_tokens=int(
                        quote.effective_input_budget_tokens * policy.auto_trigger_ratio
                    ),
                )
            finally:
                projection.close()
        tokens = payload["input_tokens"]
        payload["compaction_expected"] = (
            tokens is not None and tokens >= payload["compaction_trigger_tokens"]
        )
        return payload

    def _model_identity(self, binding: ModelCallBinding) -> str:
        return self._model_runtime.connection(binding).target.model_id


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


    async def compact_context(self, *, command_id: str) -> CompactionOutcome:
        """Accept one idle operation; a caller only waits on its existing owner."""
        if not command_id:
            raise ValueError("compaction command identity is required")
        existing = await self._query_command_row(command_id)
        if existing is not None and existing.get("command_kind") != "COMPACT_CONTEXT":
            return CompactionOutcome(CompactionDisposition.FAILED, "idle", None, None, "COMMAND_CONFLICT")
        async with self._lock:
            self._require_open()
            self._retire_done_active_root_locked()
            task = self._manual_compaction_command_attempts.get(command_id)
            if task is None and existing is None:
                if (
                    self.root_session_id is not None
                    or self._active_task is not None
                    or self._external_new_turn_accepting
                    or self._pending_root_successor is not None
                    or self._plan_exit_fence
                    or self._prompt_admission_attempts
                    or self._compaction_write_reservations
                    or self._runtime_reopen_quiescence is not None
                    or self._manual_compaction_command_attempts
                    or self._compaction.has_active_work()
                ):
                    return CompactionOutcome(CompactionDisposition.REJECTED_BUSY, "idle", None, None, "SESSION_BUSY")
                self._ephemeral.invalidate_cache_warming()
                task = self._compaction.start_owned(
                    self._prepare_idle_manual_compaction(command_id),
                    name=f"kernel-idle-compaction:{command_id}",
                )
                self._manual_compaction_command_attempts[command_id] = task
                def retire(completed):
                    if self._manual_compaction_command_attempts.get(command_id) is completed:
                        self._manual_compaction_command_attempts.pop(command_id, None)
                    self._queue_wake.set()
                    self._cell_output_wake.set()
                task.add_done_callback(retire)
        if task is not None:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise
                return CompactionOutcome(
                    CompactionDisposition.FAILED, "idle", None, None,
                    "HOST_CLOSING" if self._closing else "COMPACTION_CANCELLED",
                )
        local = await self._compaction.find_manual(command_id=command_id, scope_kind=self._session_scope_kind)
        if local is not None:
            return await asyncio.shield(local)
        return CompactionOutcome(
            CompactionDisposition.RESULT_UNAVAILABLE,
            str(existing.get("target_turn_id") or "idle"), None, None,
            "HISTORICAL_RESULT_UNAVAILABLE",
        )

    async def _prepare_idle_manual_compaction(self, command_id: str) -> CompactionOutcome:
        from pulsara_agent.conversation_kernel.direct_model import KernelModelTargetPreparationRequest
        from pulsara_agent.primitives.model_call import ModelCallPurpose
        target_turn_id = "idle"
        request = None
        try:
            # This task occupies the ordinary Host admission boundary while
            # configuration and source are selected. Queue-only writes remain
            # available; their execution waits for this exact operation.
            if await self._io.run(
                self.repository.pending_prompt_head_mode,
                session_id=self.session_id, deadline_monotonic=self._canonical_deadline(),
            ) is not None:
                return CompactionOutcome(CompactionDisposition.REJECTED_BUSY, target_turn_id, None, None, "SESSION_BUSY")
            target_turn_id = await self._io.run(
                self.repository.read_latest_terminal_scope_turn_id, self._lease.guard,
                scope_kind=ConversationScopeKind.ROOT,
                deadline_monotonic=self._canonical_deadline(),
            )
            if target_turn_id is None:
                return CompactionOutcome(CompactionDisposition.NOT_NEEDED, "idle", None, None, "NO_TERMINAL_TURN")
            snapshot = self._model_runtime.freeze_resolution_snapshot()
            binding = await self._io.run(
                self.repository.read_session_model_call_binding, self._lease.guard,
                deadline_monotonic=self._canonical_deadline(),
            )
            if binding is None:
                return CompactionOutcome(CompactionDisposition.FAILED, target_turn_id, None, None, "MODEL_CONFIGURATION_REQUIRED")
            target = self._model.prepare_target_from_snapshot(
                KernelModelTargetPreparationRequest(
                    session_id=self.session_id, turn_id=target_turn_id,
                    model_call_index=1, purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
                    maximum_input_tokens=self._runner._provider_dispatch._maximum_input_tokens_per_call,
                    binding=binding,
                ), resolution_snapshot=snapshot,
            )
            cut = await self._io.run(
                self.repository.prepare_compaction_input_cut, self._lease.guard,
                turn_id=target_turn_id, allow_terminal=True,
                deadline_monotonic=self._canonical_deadline(),
            )
            candidate = build_prepared_manual_compaction_command(
                session_id=self.session_id, command_id=command_id,
                scope_kind=self._session_scope_kind, target_turn_id=target_turn_id,
                model_call_binding=binding, source_cut=cut,
            )
            confirmation, cancelled = await self._drain_start_settlement(
                self._compaction.start_settlement(
                    self._settle_manual_compaction_command_worker(
                        candidate, operation_task=asyncio.current_task(),
                    ),
                    name=f"kernel-compaction-command:{command_id}",
                )
            )
            if confirmation is not CompactionConfirmationKind.FULL:
                return CompactionOutcome(CompactionDisposition.FAILED, target_turn_id, None, None, "COMMAND_CONFLICT")
            (request, future), request_cancelled = await self._drain_start_settlement(asyncio.create_task(
                self._compaction.request_manual(
                    command_id=command_id, scope_kind=self._session_scope_kind,
                    target_turn_id=target_turn_id, prepared_target=target,
                ),
                name=f"kernel-manual-request:{command_id}",
            ))
            if cancelled or request_cancelled or self._closing:
                raise asyncio.CancelledError
            if not await self._compaction.claim_manual_execution(request):
                return await asyncio.shield(future)
            outcome = await self._runner.compact_idle_turn(
                turn_id=target_turn_id, command_id=command_id, prepared_target=target,
            )
        except StaleHostWriter:
            outcome = CompactionOutcome(CompactionDisposition.FAILED, target_turn_id or "idle", None, None, "WRITER_REPLACED")
        except asyncio.CancelledError:
            outcome = CompactionOutcome(CompactionDisposition.FAILED, target_turn_id or "idle", None, None,
                "HOST_CLOSING" if self._closing else "COMPACTION_CANCELLED")
        except ConversationKernelConflict as error:
            outcome = CompactionOutcome(
                CompactionDisposition.REJECTED_BUSY if str(error) == "COMPACTION_BUSY" else CompactionDisposition.FAILED,
                target_turn_id or "idle", None, None,
                "SESSION_BUSY" if str(error) == "COMPACTION_BUSY" else str(error),
            )
        except Exception:
            logging.getLogger(__name__).exception("Idle compaction failed")
            outcome = CompactionOutcome(CompactionDisposition.FAILED, target_turn_id or "idle", None, None, "COMPACTION_FAILED")
        if outcome.disposition is CompactionDisposition.COMPACTED:
            self._ephemeral.invalidate()
        if request is not None:
            await self._drain_start_settlement(asyncio.create_task(
                self._compaction.settle_manual(request, outcome),
                name=f"kernel-manual-result:{command_id}",
            ))
        return outcome

    async def stop_manual_compaction(self, *, command_id: str) -> CompactionOutcome:
        """Stop the exact manual operation without inventing a turn target."""
        async with self._lock:
            task = self._manual_compaction_command_attempts.get(command_id)
            if task is not None and not task.done():
                task.cancel()
        if task is not None:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise
                return CompactionOutcome(CompactionDisposition.FAILED, "idle", None, None, "COMPACTION_CANCELLED")
        existing = await self._query_command_row(command_id)
        if existing is not None and existing.get("command_kind") == "COMPACT_CONTEXT":
            local = await self._compaction.find_manual(
                command_id=command_id, scope_kind=self._session_scope_kind,
            )
            if local is not None:
                return await asyncio.shield(local)
        return CompactionOutcome(
            CompactionDisposition.RESULT_UNAVAILABLE,
            str((existing or {}).get("target_turn_id") or "idle"), None, None,
            "HISTORICAL_RESULT_UNAVAILABLE",
        )

    async def _settle_manual_compaction_command_worker(
        self, candidate: PreparedManualCompactionCommand,
        *, operation_task: asyncio.Task | None,
    ) -> CompactionConfirmationKind:
        delay = 0.05
        write_error = None
        while True:
            try:
                await self._io.run(
                    self.repository.validate_host_writer, self._lease.guard,
                    deadline_monotonic=self._canonical_deadline(),
                )
                confirmation = await self._io.run(
                    self.repository.confirm_manual_compaction_command,
                    candidate=candidate, deadline_monotonic=self._canonical_deadline(),
                )
            except StaleHostWriter:
                raise
            except (TimeoutError, OSError, OperationalError, InterfaceError):
                await asyncio.sleep(delay)
                delay = min(delay * 2, 0.5)
                continue
            if confirmation is not CompactionConfirmationKind.NONE:
                return confirmation
            if self._closing or (operation_task is not None and operation_task.cancelling()):
                return confirmation
            if write_error is not None and not isinstance(write_error, (TimeoutError, OSError, OperationalError, InterfaceError)):
                raise write_error
            try:
                return await self._io.run(
                    self.repository.accept_manual_compaction_command,
                    self._lease.guard, candidate=candidate,
                    deadline_monotonic=self._canonical_deadline(),
                )
            except StaleHostWriter:
                raise
            except Exception as error:
                write_error = error
                await asyncio.sleep(delay)
                delay = min(delay * 2, 0.5)

    def root_compaction_in_progress(self) -> bool:
        """Disposable GUI progress, owned by the existing ROOT compaction fence."""
        return self._compaction.is_fenced(
            scope_kind=self._session_scope_kind
        )

    def active_manual_compaction_command_id(self) -> str | None:
        return next((
            command for command, task in self._manual_compaction_command_attempts.items()
            if not task.done()
        ), None)

    def current_compaction_projection(
        self,
    ) -> tuple[bool, str | None, str | None, str | None]:
        """Return the bounded same-Host manual/automatic compaction view."""

        return self._compaction.current_projection()


    def wake_queued_inputs(self) -> None:
        self._queue_wake.set()

    async def apply_scheduled_management(self, method: str, arguments: dict):
        """Compose configuration/queue management with this Host's canonical writer."""
        if method not in {"create_scheduled_task", "mutate_scheduled_task", "switch_scheduled_task", "observe_scheduled_due", "retire_missed_scheduled_due", "retire_expired_paused_once"}:
            raise ValueError("unsupported scheduled management operation")
        async with self._lock:
            self._require_open()
            reservation = self._reserve_compaction_write_locked(
                scope_kind=self._session_scope_kind)
        cancelled: list[QueuedPromptAction] = []
        if method in {"mutate_scheduled_task", "switch_scheduled_task", "retire_expired_paused_once"}:
            arguments = {**arguments, "cancelled_candidates": cancelled}
        try:
            result = await self._io.run(getattr(self.repository, method),
                guard=self._lease.guard, **arguments)
            self._queue_wake.set()
            return result
        finally:
            try:
                # The I/O owner drains admitted physical writes even on cancellation.
                # Confirm existing queue actions after an unknown COMMIT ACK; do not
                # mint a receipt or repeat configuration/queue mutations.
                for candidate in cancelled:
                    row = await self._query_command_row(candidate.command_id)
                    if row is not None and row["command_kind"] == candidate.command_kind and row["semantic_digest"] == candidate.semantic_digest and row["target_queue_item_id"] == candidate.target_queue_item_id:
                        self._hook_context.retire_prompt_candidate(scope=self._hook_root_scope, prompt_candidate_id=candidate.source_queue_item_id)
            finally:
                await self._release_compaction_write_reservation(reservation)


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
        return (
            self._active_turn_id
            if task is not None and not task.done() and self._active_turn_started
            else None
        )

    def _has_runtime_work(self) -> bool:
        manual_tasks = tuple(
            task
            for task in self._manual_compaction_command_attempts.values()
        )
        return (
            (self._active_task is not None and not self._active_task.done())
            or self._external_new_turn_accepting
            or self._pending_root_successor is not None
            or self._plan_exit_fence
            or self._compaction_write_reservations
            or any(not task.done() for task in manual_tasks)
            or self._prompt_admission_attempts
            or any(
                attempt.task is not None and not attempt.task.done()
                for attempt in self._user_control_attempts.values()
            )
            or self._capability_reload_settlement_lock.locked()
            or self._compaction.has_active_work()
            or self._code_mode.has_live_cells()
            or bool(self._code_mode.pending_output_cells())
            or not self._runner._safe_point.is_idle()
        )

    async def prepare_safe_runtime_reopen(self) -> KernelRuntimeReopenQuiescence:
        """Gate new admission and prove no accepted process-local work remains."""

        async with self._lock:
            self._require_open()
            self._retire_done_active_root_locked()
            self._retire_control_attempts_locked()
            if self._has_runtime_work():
                raise RuntimeError("HostSession is not quiescent for runtime reopen")
            quiescence = KernelRuntimeReopenQuiescence(
                session_id=self.session_id,
                host_session_id=self.host_session_id,
                writer_generation=self._lease.guard.writer_generation,
                operation_nonce=random_id("operation"),
                owner=self,
                _seal=_KERNEL_RUNTIME_REOPEN_QUIESCENCE_SEAL,
            )
            self._runtime_reopen_quiescence = quiescence
            return quiescence

    def provider_retry_observation(self) -> dict[str, object] | None:
        return None if self._closing else self._runner.provider_retry_observation()

    def archive_has_terminal_work(self) -> bool:
        return self.root_session_id is None and any(
            process.physical_state in ("RUNNING", "TERMINALIZING")
            for process in self._tools.list_background_terminal_processes()
        )

    async def inspect_archive_idle(self) -> bool:
        """Read-only UI hint; archive itself takes the admission gate."""
        async with self._lock:
            return not (self._closing or self._closed or self._runtime_reopen_quiescence
                        or self._has_runtime_work() or self.archive_has_terminal_work())

    async def abort_safe_runtime_reopen(
        self, quiescence: KernelRuntimeReopenQuiescence
    ) -> None:
        """Restore admission only for the exact uncommitted Host gate."""

        async with self._lock:
            if self._runtime_reopen_quiescence is not quiescence:
                raise RuntimeError("runtime-reopen quiescence is stale")
            quiescence._consume(self)
            self._runtime_reopen_quiescence = None
            self._queue_wake.set()
            self._cell_output_wake.set()

    async def commit_safe_runtime_reopen(
        self, quiescence: KernelRuntimeReopenQuiescence
    ) -> None:
        """Consume the exact gate while keeping admission closed for teardown."""

        async with self._lock:
            if self._runtime_reopen_quiescence is not quiescence:
                raise RuntimeError("runtime-reopen quiescence is stale")
            quiescence._consume(self)


    async def query_command(self, command_id: str) -> KernelCommandOutcome | None:
        failure = self._command_failures.get(command_id)
        row = await self._query_command_row(command_id)
        if row is None:
            return failure
        command_kind = str(row.get("command_kind") or "")
        if command_kind == "COMPACT_CONTEXT":
            local = self._manual_compaction_command_attempts.get(command_id)
            if local is not None and not local.done():
                return KernelCommandOutcome(
                    command_id, "PENDING", str(row.get("target_turn_id") or "idle"),
                    "COMPACTION_RUNNING", "Context compaction is in progress.",
                )
            outcome = None
            if local is not None and not local.cancelled():
                outcome = local.result()
            if outcome is None:
                future = await self._compaction.find_manual(
                    command_id=command_id, scope_kind=self._session_scope_kind,
                )
                if future is not None and future.done():
                    outcome = future.result()
            if outcome is None:
                return KernelCommandOutcome(
                    command_id, "REJECTED", str(row.get("target_turn_id") or "idle"),
                    "COMPACTION_RESULT_UNAVAILABLE", "The original compaction result is unavailable.",
                )
            succeeded = outcome.disposition in {CompactionDisposition.COMPACTED, CompactionDisposition.NOT_NEEDED}
            return KernelCommandOutcome(
                command_id, "SUCCEEDED" if succeeded else "REJECTED", outcome.target_turn_id,
                outcome.disposition.value, outcome.public_code,
            )
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
                handoff_created_at_commit=(interaction_kind == "DRAFT_REVIEW" and interaction_status == "CANCELLED"),
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
        status = str(row.get("turn_status") or "")
        target = str(row.get("target_turn_id") or "")
        if row.get("target_queue_item_id") is not None:
            queue_status = str(row.get("queue_status") or "")
            prompt_delivery = KernelPromptDelivery(
                queue_item_id=str(row["target_queue_item_id"]),
                queue_status=queue_status,
                consumed_entry_id=(str(row.get("consumed_entry_id") or "") or None),
                delivery_mode=str(row.get("queue_delivery_mode") or ""),
                execution=row["request_execution"],
            )
            target = str(row.get("consumed_turn_id") or row["target_queue_item_id"])
            if command_kind == "CANCEL_PROMPT" and queue_status == "CANCELLED":
                return KernelCommandOutcome(
                    command_id,
                    "SUCCEEDED",
                    target,
                    "PROMPT_CANCELLED",
                    "排队输入已取消。",
                    prompt_delivery=prompt_delivery,
                )
            execution = prompt_delivery.execution
            if execution is not None:
                command_status, code, message = {
                    RequestExecutionStatus.QUEUED: (
                        "PENDING", "PROMPT_QUEUED", "Prompt is queued.",
                    ),
                    RequestExecutionStatus.RUNNING: (
                        "PENDING", "TURN_RUNNING", "The turn is running.",
                    ),
                    RequestExecutionStatus.COMPLETED: (
                        "SUCCEEDED", "TURN_COMPLETED", "Reply accepted.",
                    ),
                    RequestExecutionStatus.STOPPED: (
                        "REJECTED", execution.terminal_reason,
                        "已改为引导。" if execution.terminal_reason == "USER_REDIRECTED_TO_STEER"
                        else "The request ended without completing.",
                    ),
                }[execution.status]
                return KernelCommandOutcome(
                    command_id, command_status, target, code, message,
                    prompt_delivery=prompt_delivery,
                )
            if queue_status == "PENDING":
                return KernelCommandOutcome(
                    command_id,
                    "PENDING",
                    target,
                    "PROMPT_STEER_QUEUED"
                    if command_kind == "STEER_QUEUED_PROMPT"
                    else "PROMPT_QUEUED",
                    "Prompt is queued.",
                    prompt_delivery=prompt_delivery,
                )
            if queue_status in {"CANCELLED", "REJECTED"}:
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    target,
                    str(row.get("queue_terminal_reason") or queue_status),
                    "已改为引导。"
                    if row.get("queue_terminal_reason") == "USER_REDIRECTED_TO_STEER"
                    else "The queued prompt was not delivered.",
                    prompt_delivery=prompt_delivery,
                )
            if queue_status == "CONSUMED":
                return KernelCommandOutcome(
                    command_id,
                    "SUCCEEDED",
                    target,
                    "STEER_DELIVERED",
                    "Steer was delivered to its target turn.",
                    prompt_delivery=prompt_delivery,
                )
            raise ValueError(f"invalid steer delivery status: {queue_status}")
        if status == "COMPLETED":
            return KernelCommandOutcome(
                command_id,
                "SUCCEEDED",
                target,
                "TURN_COMPLETED",
                "Reply accepted.",
            )
        if status == "INTERRUPTED":
            return KernelCommandOutcome(
                command_id,
                "REJECTED",
                target,
                str(row.get("terminal_reason") or "TURN_INTERRUPTED"),
                "The turn was interrupted and will not be replayed.",
            )
        return KernelCommandOutcome(
            command_id,
            "PENDING",
            target,
            "TURN_RUNNING",
            "The turn is running.",
        )

    async def attach_controller(self, attachment_id: str) -> bool:
        self._require_open()
        return await self._interactions.attach_controller(attachment_id)

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

    async def _reserve_external_new_turn(
        self,
    ) -> CompactionWriteReservation | None:
        async with self._lock:
            self._retire_done_active_root_locked()
            if (
                self._closing
                or self._plan_exit_fence
                or self._compaction.is_fenced(
                    scope_kind=self._session_scope_kind,
                )
                or self._external_new_turn_accepting
                or self._active_task is not None
                or self._manual_compaction_command_attempts
            ):
                return None
            reservation = self._reserve_compaction_write_locked(
                scope_kind=self._session_scope_kind,
            )
            self._external_new_turn_accepting = True
            self._external_new_turn_settled = asyncio.Event()
            return reservation

    async def _release_external_new_turn_reservation(self) -> None:
        async with self._lock:
            self._external_new_turn_accepting = False
            self._external_new_turn_settled.set()
            self._queue_wake.set()


    async def _observe_provider_usage(
        self,
        request: KernelModelExecutionRequest,
        report: TransportUsageReport,
        permit: ProcessLocalProviderInputInstallPermit,
        terminal_kind: ProviderNormalizedTerminalKind | None,
        anchor_allowed: bool,
    ) -> None:
        from pulsara_agent.llm.request import ProviderInputUsageAnchor
        from pulsara_agent.conversation_kernel.repository import ProviderCallUsageObservation, ProviderCallUsageWriteDisposition
        usage = report.usage
        call = request.prepared_call.call
        plan = request.wire_input_plan
        diagnostic_codes = tuple(item.code for item in report.provider_diagnostics)
        if terminal_kind is None:
            diagnostic_codes += ("provider_terminal_not_observed",)
        if usage is not None and usage.input_tokens == 0:
            diagnostic_codes += ("provider_input_zero_nonempty",)
        anchor = None
        if anchor_allowed and usage is not None and usage.input_tokens is not None and usage.input_tokens > 0:
            anchor = ProviderInputUsageAnchor(
                model_call_id=call.resolved_model_call_id,
                epoch_nonce=permit.epoch_nonce, epoch_revision=permit.epoch_revision,
                connection_id=call.binding.connection_id.value, target=call.target.fact,
                route_wire_profile_fingerprint=plan.route_wire_profile_fingerprint,
                reported_model_id=report.reported_model_id,
                reported_input_tokens=usage.input_tokens,
                raw_input_tokens=plan.quote.raw_final_wire_estimated_input_tokens,
                materialization=plan.materialization,
            )
        self._input_continuity.observe_usage_anchor(
            permit.scope, epoch_nonce=permit.epoch_nonce, epoch_revision=permit.epoch_revision,
            reported_model_id=report.reported_model_id, anchor=anchor,
        )
        observation = ProviderCallUsageObservation(
            session_id=request.session_id, turn_id=request.turn_id,
            resolved_model_call_id=call.resolved_model_call_id,
            model_call_index=request.model_call_index,
            connection_id=call.binding.connection_id.value, route_id=call.target.fact.route_id,
            wire_api=plan.wire_api, requested_model_id=call.target.fact.model_id,
            reported_model_id=report.reported_model_id,
            normalized_terminal_kind=None if terminal_kind is None else terminal_kind.value,
            usage_status=report.usage_status,
            input_tokens=None if usage is None else usage.input_tokens,
            output_tokens=None if usage is None else usage.output_tokens,
            cached_input_tokens=None if usage is None else usage.cached_input_tokens,
            reasoning_output_tokens=None if usage is None else usage.reasoning_output_tokens,
            reported_total_tokens=None if usage is None else usage.reported_total_tokens,
            diagnostic_codes=diagnostic_codes,
        )
        try:
            disposition = await self._io.run(
                self.repository.record_provider_call_usage, observation,
                deadline_monotonic=self._canonical_deadline(),
            )
            if disposition is ProviderCallUsageWriteDisposition.CONFLICT:
                diagnostic_codes += ("provider_usage_storage_conflict",)
                logging.getLogger(__name__).warning("provider_usage_storage_conflict")
        except Exception:
            diagnostic_codes += ("provider_usage_storage_failed",)
            logging.getLogger(__name__).warning("provider_usage_storage_failed")
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
                    "reported_total_tokens": None if usage is None else usage.reported_total_tokens,
                    "computed_total_tokens": None if usage is None else usage.computed_total_tokens,
                    "reported_model_id": report.reported_model_id,
                    "diagnostic_codes": diagnostic_codes,
                },
            )
        )

    def _require_open(self) -> None:
        if self.writer_unavailable:
            if self.writer_lost:
                raise StaleHostWriter("Host writer is unavailable; reopen this runtime")
            raise RuntimeError(
                "Host writer renewal failed; reopen this runtime"
            ) from self._writer_renewal_failure
        if self._closing:
            raise RuntimeError("kernel Host session is closing")
        if getattr(self, "_runtime_reopen_quiescence", None) is not None:
            raise RuntimeError("kernel Host session is gated for runtime reopen")


class KernelHostCore(_HostCoreManagementOperations):
    """Process owner for verified PostgreSQL and live Host sessions."""

    def __init__(
        self,
        *,
        model_runtime: ModelRuntime,
        memory_domain_id: str = "u_local",
        permission_policy: EffectivePermissionPolicy | None = None,
        active_skill_names: frozenset[str] = frozenset(),
        trust_workspace_mcp_config: bool = False,
        authenticated_first_party_extension_ids: frozenset[str] = frozenset(),
        watchdog_policy: KernelExecutionWatchdogPolicy | None = None,
        credential_boundary: ProcessCredentialBoundary | None = None,
        open_browser: Callable[[str], Awaitable[None]] | None = None,
        workspace_validator: Callable[[Path], None] | None = None,
    ) -> None:
        self._model_runtime = model_runtime
        self._memory_domain_id = memory_domain_id
        self._root_permission_policy = permission_policy or default_permission_policy()
        self._root_active_skill_names = active_skill_names
        self._trust_workspace_mcp_config = trust_workspace_mcp_config
        self._workspace_validator = workspace_validator
        self._deadlines = KernelExecutionDeadlineFactory(
            watchdog_policy or DEFAULT_KERNEL_WATCHDOG_POLICY
        )
        self._image_validator = HostPromptImageValidator()
        self._access: VerifiedPostgresAccessLease | None = None
        self._repository: ConversationKernelRepository | None = None
        self._asr_cache = None
        self._media_asr_runtime = None
        self._resource_initialization: _CoreResourceInitialization | None = None
        self._resource_operations: dict[asyncio.Task[object], float] = {}
        self._blob_store: PostgresCanonicalBlobStore | None = None
        self._blob_gc_io: KernelSessionIO | None = None
        self._blob_gc_task: asyncio.Task[None] | None = None
        self._history_maintenance: HistoryProjectionMaintainer | None = None
        self._sessions: dict[str, KernelHostSession] = {}
        self._agent_runtime = AgentSessionRuntime(self)
        self._suspended_agent_roots: set[str] = set()
        self._workspace_capability_revisions: dict[Path, int] = {}
        self._open_attempts: dict[asyncio.Future[None], KernelSessionOpenAttempt] = {}
        self._session_retirements: dict[str, KernelSessionRetirement] = {}
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
        from pulsara_agent.scheduling.service import ScheduledTaskService
        self._scheduled_tasks = ScheduledTaskService(self)
        self.mcp_management = LocalMcpManagementService(
            model_runtime.settings, credential_boundary=self._credential_boundary,
            open_browser=open_browser,
        )

    def _live_agent_session(self, session_id: str) -> KernelHostSession | None:
        for session in self._sessions.values():
            if session.session_id == session_id and not (session._closing or session._closed):
                return session
        return None

    async def _load_agent_session(self, row: Mapping[str, object]) -> KernelHostSession:
        session_id = str(row["id"])
        live = self._live_agent_session(session_id)
        if live is not None:
            live.wake_queued_inputs()
            return live
        from pulsara_agent.tool_permission import preset_to_policy
        workspace_input = HostWorkspaceInput(
            workspace_kind=str(row["workspace_kind"]),
            workspace_root=str(row["workspace_root"]),
            display_label=(str(row.get("workspace_label") or "Agent")
                           if row["workspace_kind"] == "transient" else None),
            memory_domain_id=str(row["memory_domain_id"]),
        )
        settlement = await self._admit_session_open(session_id, str(row["root_session_id"]))
        try:
            return await self.resume_session(
                session_id,
                workspace_input=workspace_input,
                permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS),
                active_skill_names=frozenset(),
            )
        finally:
            await self._settle_session_open(settlement)

    async def _send_agent_message(self, agent_id: str, message: str, context) -> dict[str, object]:
        from pulsara_agent.primitives.context import canonical_json_bytes
        from pulsara_agent.primitives.tool_result_budget import ToolResultPageBoundary

        recipient = {"kind": "AGENT", "agent_id": agent_id}
        def accepted_receipt(work_id):
            return {"recipient": recipient,
                "work": None if work_id is None else {"kind": "WORK", "work_id": work_id},
                "delivery": "queued", "message": "Message accepted by the active turn."}
        def check_receipt(work_id):
            if not context.page_budget.fits(canonical_json_bytes(accepted_receipt(work_id)).decode("utf-8")):
                raise ToolResultPageBoundary("The agent message receipt does not fit this call's result allowance")
        live = self._live_agent_session(agent_id)
        if live is None:
            repository = await self._ensure_resources()
            state = await asyncio.to_thread(repository.read_agent_runtime_state,
                session_id=agent_id, deadline_monotonic=self._canonical_deadline())
            pending = state is not None and state["has_pending_work"]
            return {"recipient": recipient, "work": None,
                    "delivery": "not_started" if pending else "inactive",
                    "message": "Message was not accepted: the recipient has no active turn."}
        async with live._lock:
            turn_id = live.active_root_turn_id()
        if turn_id is None:
            state = await live._io.run(live.repository.read_agent_runtime_state,
                session_id=agent_id, deadline_monotonic=live._canonical_deadline())
            return {"recipient": recipient, "work": None,
                    "delivery": "not_started" if state is not None and state["has_pending_work"] else "inactive",
                    "message": "Message was not accepted: the recipient has no active turn."}
        # Exact turn identity is captured once. Repository admission serializes
        # against ordinary assistant completion and never redirects to a successor.
        accepted = await live._io.run(
            live.repository.enqueue_agent_steer, live._lease.guard,
            target_turn_id=turn_id, queue_item_id=random_id("queue"),
            source_session_id=context.session_id,
            source_work_id=getattr(context, "work_id", None), text=message,
            occurred_at=datetime.now(timezone.utc), actor_id=context.session_id,
            deadline_monotonic=live._canonical_deadline(),
            result_check=check_receipt,
        )
        if accepted is None:
            return {"recipient": recipient, "work": None, "delivery": "inactive",
                    "message": "Message was not accepted: the recipient turn ended."}
        live.wake_queued_inputs()
        await self._agent_runtime.notify_changed()
        return accepted_receipt(accepted.get("work_id"))


    async def _stop_agent_active_work(self, agent_id: str, work_id: str | None = None) -> None:
        live = self._live_agent_session(agent_id)
        if live is None:
            return
        if work_id is not None:
            row = await live._io.run(live.repository.query_agent_work,
                root_session_id=live.root_session_id or live.session_id,
                work_id=work_id, deadline_monotonic=live._canonical_deadline())
            if row is None:
                return
        async with live._lock:
            task = live._active_task
            intent = live._active_cancellation_intent
            if (task is None or task.done() or intent is None
                or (work_id is not None and live._active_work_id != work_id)):
                return
            intent.install_cause(ForegroundCancellationCause.USER_REQUEST)
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await live._settle_active_root_task(task)
        await self._agent_runtime.notify_changed()

    async def _retire_idle_agent(self, row: Mapping[str, object], *, delete: bool) -> None:
        from pulsara_agent.conversation_kernel.subagent import AGENT_DELETE_IDLE, AGENT_UNLOAD_IDLE
        session_id = str(row["id"])
        try:
            operation = await self.begin_session_retirement(session_id, str(row["memory_domain_id"]))
        except SessionDeleteRejected:
            return
        gate = None
        live = None
        committed = False
        try:
            live = self._live_agent_session(session_id)
            if live is not None:
                try:
                    gate = await live.prepare_safe_runtime_reopen()
                except RuntimeError:
                    return
                if live.archive_has_terminal_work():
                    return
            repository = await self._ensure_resources()
            cutoff = datetime.now(timezone.utc) - (AGENT_DELETE_IDLE if delete else AGENT_UNLOAD_IDLE)
            state = await self._agent_runtime._io.run(repository.read_agent_runtime_state,
                session_id=session_id, deadline_monotonic=self._canonical_deadline())
            if (state is None or state["has_pending_work"] or state["has_running_turn"]
                or state.get("idle_since") is None or state["idle_since"] > cutoff):
                return
            if live is not None:
                await live.commit_safe_runtime_reopen(gate)
                committed = True
            await self.quiesce_session_retirement(operation)
            if delete:
                await self.commit_session_deletion(operation, idle_before=cutoff)
        finally:
            if gate is not None and not committed and live is not None and not live._closing:
                await live.abort_safe_runtime_reopen(gate)
            await self.finish_session_retirement(operation, quarantine=False)

    def _canonical_deadline(self) -> float:
        current = asyncio.current_task()
        deadline = self._deadlines.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL)
        if current in self._resource_operations:
            # Observe the actual existing operation boundary; do not turn
            # a compound Core call into a new total-duration budget.
            self._resource_operations[current] = deadline
        return deadline

    @property
    def model_runtime(self) -> ModelRuntime:
        return self._model_runtime

    def _retire_resource_operation(self, task: asyncio.Task[object]) -> None:
        self._resource_operations.pop(task, None)
        if not task.cancelled():
            task.exception()

    @property
    def has_database_resources(self) -> bool:
        """Whether database maintenance must close this core before deletion."""
        return (self._access is not None or self._repository is not None
                or (self._resource_initialization is not None
                    and not self._resource_initialization.cleanup_complete))

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
        mcp_scope_policies=(),
    ) -> PluginInstallOutcome:
        """Install one local package into the user Plugin store, initially disabled."""

        from pulsara_agent.mcp_config import McpScopePolicy

        request = InstallLocalPluginRequest(
            source_path=source_path,
            scope=PluginScopeKind.USER,
            deadline_monotonic=self._canonical_deadline(),
            source_format=source_format,
            import_classifications=import_classifications,
            import_public_values=import_public_values,
            mcp_scope_policies=tuple((name, McpScopePolicy(policy)) for name, policy in mcp_scope_policies),
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
        memory_domain_id: str = "u_local",
        permission_policy: EffectivePermissionPolicy | None = None,
        active_skill_names: frozenset[str] = frozenset(),
        trust_workspace_mcp_config: bool = False,
        authenticated_first_party_extension_ids: frozenset[str] = frozenset(),
        watchdog_policy: KernelExecutionWatchdogPolicy | None = None,
        credential_boundary: ProcessCredentialBoundary | None = None,
        open_browser: Callable[[str], Awaitable[None]] | None = None,
        workspace_validator: Callable[[Path], None] | None = None,
    ) -> "KernelHostCore":
        return cls(
            model_runtime=model_runtime,
            memory_domain_id=memory_domain_id,
            permission_policy=permission_policy,
            active_skill_names=active_skill_names,
            trust_workspace_mcp_config=trust_workspace_mcp_config,
            authenticated_first_party_extension_ids=(
                authenticated_first_party_extension_ids
            ),
            watchdog_policy=watchdog_policy,
            credential_boundary=credential_boundary,
            open_browser=open_browser,
            workspace_validator=workspace_validator,
        )

    async def start_scheduled_tasks(self) -> None:
        """Admit the one timer after resource and model preparation is usable."""
        async with self._lock:
            if self._closing:
                raise KernelHostCoreClosing("Kernel Host core is closing")
            if self._repository is None:
                return
        # Saved custom models can be usable without a remote catalog. The
        # runtime's actual resolution facts, rather than a UI loading flag,
        # decide whether this process can prepare scheduled inputs.
        try:
            resolution = self._model_runtime.freeze_resolution_snapshot()
        except ModelRuntimeUnavailable:
            return
        if not resolution.resolved:
            return
        await self.scheduled_tasks.start()

    async def notify_scheduled_tasks_changed(self) -> None:
        """Reconsider configured resources and wake the existing timer owner."""
        await self.start_scheduled_tasks()
        self.scheduled_tasks.wake()

    @property
    def root_permission_policy(self) -> EffectivePermissionPolicy:
        return self._root_permission_policy

    @property
    def memory_domain_id(self) -> str:
        return self._memory_domain_id

    @property
    def scheduled_tasks(self):
        return self._scheduled_tasks

    async def asr_cache_owner(self):
        """User-level cache owner; acquiring it never requires an active session."""
        await self._ensure_resources()
        return self._asr_cache

    @_core_database_operation
    async def touch_asr_cache_source(self, source_path: str, source_modified_ns: int, source_size: int) -> None:
        """Record an actual, already-authorized media preview/read start."""
        from pulsara_agent.media.asr_cache import AsrMediaKey
        owner = await self.asr_cache_owner()
        await asyncio.to_thread(owner.touch,
            AsrMediaKey(self.memory_domain_id, source_path, source_modified_ns, source_size),
            deadline_monotonic=self._canonical_deadline())

    @_core_database_operation
    async def clear_old_asr_cache(self) -> dict[str, object]:
        owner = await self.asr_cache_owner()
        count = await asyncio.to_thread(owner.clear_old, self.memory_domain_id,
            deadline_monotonic=self._canonical_deadline())
        return {"deleted_blocks": count, "message": (
            f"已清理 {count} 个旧转写缓存块。" if count
            else "暂无可清理的旧转写缓存")}

    @property
    def root_active_skill_names(self) -> frozenset[str]:
        return self._root_active_skill_names

    @property
    def trust_workspace_mcp_config(self) -> bool:
        return self._trust_workspace_mcp_config

    async def prepare_resources(self) -> None:
        """Verify and acquire cold database resources without reading a UI list."""
        await self._ensure_resources()
        await self.start_scheduled_tasks()

    async def _ensure_resources(self) -> ConversationKernelRepository:
        async with self._lock:
            if (self._closing and asyncio.current_task() not in self._resource_operations
                    and not self._current_session_open_admitted()):
                raise KernelHostCoreClosing("Kernel Host core is closing")
            if self._repository is not None:
                return self._repository
            initialization = self._resource_initialization
            task = initialization.task if initialization is not None else None
            if task is None or (task.done() and initialization.cleanup_complete
                                and (task.cancelled() or task.exception() is not None)):
                initialization = _CoreResourceInitialization(self._canonical_deadline())
                task = asyncio.create_task(self._initialize_resources(initialization), name="kernel-core-resources")
                initialization.task = task
                self._resource_initialization = initialization
                task.add_done_callback(lambda completed: None if completed.cancelled() else completed.exception())
        return await asyncio.shield(task)

    async def _initialize_resources(self, initialization: _CoreResourceInitialization) -> ConversationKernelRepository:
        access = history = io_owner = None
        try:
            try:
                postgres = self._model_runtime.settings.read().postgres
            except Exception as exc:
                raise KernelCompositionUnavailable(
                    "local PostgreSQL settings are unavailable"
                ) from exc
            if postgres is None:
                raise KernelCompositionUnavailable("PostgreSQL is not configured")
            access = await process_postgres_schema_verification_service().acquire(
                postgres.runtime_dsn,
                deadline_monotonic=initialization.deadline_monotonic,
            )
            initialization.access = access
            initialization.deadline_monotonic = self._canonical_deadline()
            await asyncio.to_thread(
                require_stage2_runtime_privilege_boundary,
                access.connection_provider,
                deadline_monotonic=initialization.deadline_monotonic,
            )
            repository = ConversationKernelRepository(
                access.connection_provider,
                post_commit_tap=self._route_committed_events_from_thread,
            )
            blob_store = PostgresCanonicalBlobStore(
                access.connection_provider
            )
            history = HistoryProjectionMaintainer(
                access.connection_provider
            )
            initialization.history = history
            io_owner = KernelSessionIO(maximum_concurrency=1)
            initialization.io = io_owner
            async with self._lock:
                if self._closing:
                    raise KernelHostCoreClosing("Kernel Host shutdown won resource initialization")
                self._access = access
                self._repository = repository
                from pulsara_agent.media.asr_cache import PostgresAsrCache
                self._asr_cache = PostgresAsrCache(access.connection_provider)
                from pulsara_agent.media.runtime import MediaAsrRuntime
                self._media_asr_runtime = MediaAsrRuntime()
                self._blob_store = blob_store
                self._history_maintenance = history
                self._blob_gc_io = io_owner
                self._event_loop = asyncio.get_running_loop()
                self._blob_gc_task = asyncio.create_task(
                    self._blob_gc_loop(), name="kernel-blob-orphan-gc"
                )
                initialization.access = initialization.history = initialization.io = None
                initialization.cleanup_complete = True
            return repository
        except BaseException:
            for slot, owner in (("io", io_owner), ("history", history)):
                if owner is not None:
                    try:
                        await owner.aclose(deadline_monotonic=self._canonical_deadline())
                    except BaseException:
                        logging.getLogger(__name__).exception("cold resource cleanup failed")
                        if not owner.physical_close_complete:
                            continue
                    setattr(initialization, slot, None)
            initialization.cleanup_complete = initialization.io is None and initialization.history is None
            if access is not None and initialization.cleanup_complete:
                access.release()
                initialization.access = None
            raise

    @_core_database_operation
    async def fork_conversation(
        self,
        *,
        source_session_id: str,
        anchor_entry_id: str,
        child_session_id: str,
        memory_domain_id: str,
        deadline_monotonic: float | None = None,
    ):
        """Canonical copy only. Opening the committed child uses ordinary resume."""
        settlement = await self._admit_session_open(source_session_id, child_session_id)
        try:
            summary = await self.read_resumable_session(source_session_id, memory_domain_id=memory_domain_id)
            if summary is None:
                raise KeyError(source_session_id)
            require_workspace(summary.workspace_root)
            repository = await self._ensure_resources()
            deadline = self._canonical_deadline() if deadline_monotonic is None else deadline_monotonic
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
            creation = worker.result()
            if creation.created and self._history_maintenance is not None:
                self._history_maintenance.offer_session(child_session_id)
            if cancelled is not None:
                raise cancelled
            return creation
        finally:
            await self._settle_session_open(settlement)

    async def open_session(
        self,
        workspace_input: HostWorkspaceInput,
        *,
        permission_policy: EffectivePermissionPolicy | None = None,
        system_prompt: str | None = None,
        active_skill_names: frozenset[str] | None = None,
    ) -> KernelHostSession:
        return await self._open(
            workspace_input,
            session_id=random_id("session"),
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
        active_skill_names: frozenset[str] | None = None,
        restore_missing_workspace: bool = False,
        scheduled: bool = False,
        operation: asyncio.Future[None] | None = None,
    ) -> KernelHostSession:
        return await self._open(
            workspace_input,
            session_id=session_id,
            permission_policy=permission_policy,
            system_prompt=system_prompt,
            active_skill_names=active_skill_names,
            session_start_source="resume",
            restore_missing_workspace=restore_missing_workspace,
            scheduled=scheduled,
            operation=operation,
        )

    async def resume_scheduled_session(self, session_id: str) -> KernelHostSession:
        """Restore a selected ROOT through the same kernel open owner as the UI."""
        async with self._lock:
            self._require_session_control_available_locked(session_id)
            pending = any(session_id in attempt.session_ids
                          and (attempt.task is None or attempt.configuration is not None)
                          for attempt in self._open_attempts.values())
            live = next((item for item in self._sessions.values() if item.session_id == session_id), None)
            if live is not None and live._memory_domain_id != self.memory_domain_id:
                raise KeyError(session_id)
            if not pending and live is not None and not live.writer_unavailable:
                return live
        summary = await self.read_resumable_session(
            session_id, memory_domain_id=self.memory_domain_id,
        )
        if summary is None:
            raise KeyError(session_id)
        return await self.resume_session(
            session_id,
            workspace_input=HostWorkspaceInput(
                workspace_kind=summary.workspace_kind,
                workspace_root=summary.workspace_root,
                display_label=(summary.workspace_label if summary.workspace_kind != "project" else None),
                memory_domain_id=summary.memory_domain_id,
                cleanup_workspace_root_on_close=False,
                trust_workspace_mcp_config=self._trust_workspace_mcp_config,
            ),
            scheduled=True,
        )

    async def scheduled_management_host(self, session_id: str, *, memory_domain_id: str | None = None) -> KernelHostSession | None:
        """Locate the canonical live writer without opening a session for management."""
        async with self._lock:
            self._require_session_control_available_locked(session_id)
            if any(session_id in attempt.session_ids
                   and (attempt.task is None or attempt.configuration is not None)
                   for attempt in self._open_attempts.values()):
                raise SessionControlRejected("SESSION_CONTROL_BUSY", "会话正在处理另一项操作。")
            live = next((item for item in self._sessions.values() if item.session_id == session_id), None)
            if live is not None and live._memory_domain_id != (memory_domain_id or self.memory_domain_id):
                raise KeyError(session_id)
            return live

    async def reserve_session_operation(self, session_id: str) -> asyncio.Future[None]:
        """Fence kernel execution while a host-facing lifecycle operation settles."""
        async with self._lock:
            self._require_session_control_available_locked(session_id)
            if any(session_id in attempt.session_ids
                   and (attempt.task is None or attempt.configuration is not None)
                   for attempt in self._open_attempts.values()):
                raise SessionControlRejected("SESSION_CONTROL_BUSY", "会话正在处理另一项操作。")
            settlement = asyncio.get_running_loop().create_future()
            self._open_attempts[settlement] = KernelSessionOpenAttempt(frozenset((session_id,)))
            return settlement

    async def release_session_operation(self, operation: asyncio.Future[None]) -> None:
        await self._settle_session_open(operation)

    def _require_session_control_available_locked(self, session_id: str) -> None:
        if self._closing:
            raise KernelHostCoreClosing("Kernel Host core is closing")
        if session_id in self._session_retirements:
            raise SessionDeleteRejected("SESSION_DELETE_BUSY", "会话正在删除。")
        for attempt in self._close_attempts.values():
            if attempt.session.session_id == session_id:
                raise SessionControlRejected(
                    "SESSION_RECOVERY_CLOSE_FAILED" if attempt.state is HostSessionCloseState.CLOSE_FAILED_QUARANTINED else "SESSION_CONTROL_BUSY",
                    "旧会话仍在停止，请等待停止完成后再试。",
                )

    def _current_session_open_admitted(self) -> bool:
        current = asyncio.current_task()
        return any(attempt.task is current for attempt in self._open_attempts.values())

    def _session_open_children_stopped(self) -> bool:
        # Before construction, the final post-await closing check prevents any
        # future spawn. After construction the existing open attempt retains
        # the complete candidate until publication or physical cleanup.
        return all(attempt.candidate_session is None
                   or attempt.candidate_session.local_children_stopped
                   for attempt in self._open_attempts.values()
                   if attempt.configuration is not None)

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
        active_skill_names: frozenset[str] | None,
        session_start_source: str,
        restore_missing_workspace: bool = False,
        scheduled: bool = False,
        operation: asyncio.Future[None] | None = None,
    ) -> KernelHostSession:
        normalized_workspace = replace(workspace_input, workspace_root=(
            str(Path(workspace_input.workspace_root).expanduser().absolute())
            if workspace_input.workspace_root is not None else None
        ))
        effective_policy = permission_policy or self._root_permission_policy
        effective_skills = self._root_active_skill_names if active_skill_names is None else active_skill_names
        configuration = (normalized_workspace, effective_policy, system_prompt, effective_skills)
        async with self._lock:
            self._require_session_control_available_locked(session_id)
            if operation is not None:
                reserved = self._open_attempts.get(operation)
                if reserved is None or reserved.session_ids != frozenset((session_id,)) or reserved.task is not None:
                    raise RuntimeError("session operation is not current")
            current = asyncio.current_task()
            previous = next((item for item in self._sessions.values() if item.session_id == session_id), None)
            pending = [attempt for token, attempt in self._open_attempts.items()
                       if session_id in attempt.session_ids and token is not operation
                       and attempt.task is not current
                       and (previous is None or attempt.task is None or attempt.configuration is not None)]
            if pending:
                attempt = pending[0]
                if attempt.task is None or (scheduled and not attempt.scheduled):
                    raise SessionControlRejected("SESSION_CONTROL_BUSY", "会话正在打开或恢复，请稍后再试。")
                if attempt.configuration != configuration:
                    raise ValueError("session open configuration conflicts with its current owner")
                task = attempt.task
            else:
                if previous is not None and not previous.writer_unavailable:
                    if previous._closing or previous._closed:
                        raise SessionControlRejected("SESSION_CONTROL_BUSY", "会话仍在停止。")
                    saved = previous.workspace
                    if (normalized_workspace.memory_domain_id != previous._memory_domain_id
                            or normalized_workspace.workspace_kind != saved.workspace_kind
                            or normalized_workspace.workspace_root != str(saved.workspace_root)
                            or normalized_workspace.trust_workspace_mcp_config != saved.trust_workspace_mcp_config):
                        raise ValueError("session workspace conflicts with its current owner")
                    if permission_policy is not None and mode_for_policy(permission_policy) != previous._launch_permission_mode:
                        raise ValueError("session permission configuration conflicts with its current owner")
                    if active_skill_names is not None and active_skill_names != previous._capabilities.configured_active_skill_names:
                        raise ValueError("session skill configuration conflicts with its current owner")
                    if system_prompt is not None and system_prompt != previous._context_sources._base:
                        raise ValueError("session system configuration conflicts with its current owner")
                    return previous
                settlement = asyncio.get_running_loop().create_future()
                task = asyncio.create_task(self._open_owner(
                    settlement=settlement, previous=previous,
                    workspace_input=workspace_input, session_id=session_id,
                    permission_policy=effective_policy, system_prompt=system_prompt,
                    active_skill_names=effective_skills,
                    session_start_source=session_start_source,
                    restore_missing_workspace=restore_missing_workspace, scheduled=scheduled,
                ), name=f"kernel-session-open:{session_id}")
                self._open_attempts[settlement] = KernelSessionOpenAttempt(
                    frozenset((session_id,)), task=task, scheduled=scheduled, configuration=configuration,
                )
                task.add_done_callback(lambda completed: None if completed.cancelled() else completed.exception())
        return await asyncio.shield(task)

    async def _open_owner(
        self, *, settlement: asyncio.Future[None], previous: KernelHostSession | None,
        workspace_input: HostWorkspaceInput, session_id: str,
        permission_policy: EffectivePermissionPolicy | None, system_prompt: str | None,
        active_skill_names: frozenset[str], session_start_source: str,
        restore_missing_workspace: bool, scheduled: bool,
    ) -> KernelHostSession:
        try:
            if previous is not None:
                try:
                    await self.close_session(previous.host_session_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    raise SessionControlRejected(
                        "SESSION_RECOVERY_CLOSE_FAILED", "旧会话仍在停止，请等待停止完成后再试。",
                    ) from exc
                async with self._lock:
                    if self._closing:
                        raise KernelHostCoreClosing("Kernel Host core is closing")
            return await self._open_admitted(
                workspace_input,
                session_id=session_id,
                permission_policy=permission_policy,
                system_prompt=system_prompt,
                active_skill_names=active_skill_names,
                session_start_source=session_start_source,
                restore_missing_workspace=restore_missing_workspace,
                scheduled=scheduled,
            )
        finally:
            await self._settle_session_open(settlement)

    async def _admit_session_open(self, *session_ids: str) -> asyncio.Future[None]:
        async with self._lock:
            if self._closing:
                raise KernelHostCoreClosing("Kernel Host core is closing")
            if any(s in self._session_retirements for s in session_ids):
                raise SessionDeleteRejected("SESSION_DELETE_BUSY", "会话正在删除。")
            settlement = asyncio.get_running_loop().create_future()
            self._open_attempts[settlement] = KernelSessionOpenAttempt(
                frozenset(session_ids), task=asyncio.current_task(),
            )
            return settlement

    async def _settle_session_open(self, settlement: asyncio.Future[None]) -> None:
        async with self._lock:
            self._open_attempts.pop(settlement, None)
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
        restore_missing_workspace: bool = False,
        scheduled: bool = False,
    ) -> KernelHostSession:
        workspace = resolve_workspace(
            workspace_input,
            intent="RESTORE" if restore_missing_workspace else
                   "NEW" if session_start_source == "startup" else "EXISTING",
        )
        if self._workspace_validator is not None and workspace.workspace_kind == "project":
            # The ordinary resolver may resolve links; WSL admission must reject
            # that changed spelling before any database/capability side effect.
            if Path(workspace_input.workspace_root) != workspace.workspace_root:
                raise ValueError("WSL workspace path identity changed")
            self._workspace_validator(workspace.workspace_root)
        repository = await self._ensure_resources()
        deadline = self._canonical_deadline()
        async with self._lock:
            host_id = random_id("host")
            while host_id in self._sessions:
                host_id = random_id("host")
        io_owner = KernelSessionIO()
        attempt = next((item for item in self._open_attempts.values()
                        if item.task is asyncio.current_task() and item.configuration is not None), None)
        if attempt is None:
            raise RuntimeError("Session writer acquisition has no admitted open owner")
        attempt.io_owner = io_owner
        writer_lease = None
        try:
            while True:
                try:
                    writer_lease = await io_owner.run(
                        repository.acquire_host_writer,
                        intent="RESTORE" if restore_missing_workspace else
                               "SCHEDULED" if scheduled else
                               "NEW" if session_start_source == "startup" else "EXISTING",
                        session_id=session_id, workspace_id=workspace.workspace_key,
                        workspace_kind=workspace.workspace_kind,
                        workspace_root=str(workspace.workspace_root),
                        workspace_label=workspace.display_label,
                        memory_domain_id=workspace.memory_domain.memory_domain_id,
                        writer_owner_id=host_id,
                        lease_seconds=self._deadlines.policy.writer_lease_seconds,
                        deadline_monotonic=deadline,
                    )
                    break
                except UnpublishedIdentityCollision:
                    if session_start_source != "startup":
                        raise
                    if monotonic() >= deadline:
                        raise TimeoutError("session allocation deadline expired")
                    session_id = random_id("session")
            created_at = None
            if restore_missing_workspace:
                renewed_lease = await io_owner.run(
                    repository.renew_host_writer, writer_lease.guard,
                    memory_domain_id=workspace.memory_domain.memory_domain_id,
                    lease_seconds=self._deadlines.policy.writer_lease_seconds,
                    deadline_monotonic=deadline,
                )
                # Keep the original acquisition fact for the cold-epoch bootstrap.
                writer_lease = replace(writer_lease, expires_at=renewed_lease.expires_at)
                filesystem = asyncio.create_task(asyncio.to_thread(_restore_workspace_directory, workspace.workspace_root))
                _, cancelled, _ = await _join_task_beyond_logical_deadline(filesystem, deadline_monotonic=deadline)
                created = filesystem.result()
                if cancelled is not None:
                    raise cancelled
                if created:
                    created_at = datetime.now(timezone.utc)
                renewed_lease = await io_owner.run(
                    repository.renew_host_writer, writer_lease.guard,
                    memory_domain_id=workspace.memory_domain.memory_domain_id,
                    lease_seconds=self._deadlines.policy.writer_lease_seconds,
                    deadline_monotonic=deadline,
                )
                # Keep the original acquisition fact for the cold-epoch bootstrap.
                writer_lease = replace(writer_lease, expires_at=renewed_lease.expires_at)
            require_workspace(workspace.workspace_root)
            session = await self._build_open_session(
                workspace, session_id=session_id, host_id=host_id,
                io_owner=io_owner, writer_lease=writer_lease,
                permission_policy=permission_policy, system_prompt=system_prompt,
                active_skill_names=active_skill_names, session_start_source=session_start_source,
            )
            if created_at is not None:
                await session.record_workspace_recreated(created_at)
            return session
        except BaseException:
            candidate = next((attempt.candidate_session for attempt in self._open_attempts.values()
                              if attempt.task is asyncio.current_task() and attempt.candidate_session is not None), None)
            if candidate is not None:
                # The same prepared/live close attempt now owns this writer
                # and IO. Never release either behind an unjoined candidate.
                raise
            if writer_lease is not None:
                try:
                    await asyncio.to_thread(repository.release_host_writer, writer_lease.guard,
                                            deadline_monotonic=self._canonical_deadline())
                except Exception:
                    logging.getLogger(__name__).exception("failed to release prepared session writer")
            await io_owner.aclose(deadline_monotonic=self._canonical_deadline())
            raise

    async def _build_open_session(
        self, workspace: ResolvedWorkspace, *, session_id: str, host_id: str,
        io_owner: KernelSessionIO, writer_lease: WriterLease,
        permission_policy: EffectivePermissionPolicy | None, system_prompt: str | None,
        active_skill_names: frozenset[str], session_start_source: str,
    ) -> KernelHostSession:
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
            plugin_view_owner.observe_for_runtime,
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
            mcp_normalization = await _shielded_plugin_filesystem_call(
                normalize_plugin_mcp_configs,
                close_result=lambda value: value.close_plugin_anchors(),
                existing_configs=local_mcp_configs,
                view=initial_plugin_view,
                secret_resolver=self.mcp_management.settings.read().mcp_secret,
                oauth_bind=self.mcp_management.bind_authorization,
                current_state=plugin_view_owner.current_state,
            )
            mcp_configs = mcp_normalization.configs
            repository = await self._ensure_resources()
        except BaseException:
            initial_plugin_view.close()
            raise
        try:
            renewed_lease = await io_owner.run(
                repository.renew_host_writer, writer_lease.guard,
                memory_domain_id=workspace.memory_domain.memory_domain_id,
                lease_seconds=self._deadlines.policy.writer_lease_seconds,
                deadline_monotonic=self._canonical_deadline(),
            )
            # Keep the original acquisition fact for the cold-epoch bootstrap.
            writer_lease = replace(writer_lease, expires_at=renewed_lease.expires_at)
            require_workspace(workspace.workspace_root)
            root_session_id = await io_owner.run(
                repository.read_session_root_id,
                session_id=session_id,
                deadline_monotonic=self._canonical_deadline(),
            )
            # This is the last await before Session construction can install
            # live owners. Closing wins here or observes the exact candidate
            # below; no unregistered owner can appear after exit was admitted.
            if self._closing:
                raise KernelHostCoreClosing("Kernel Host shutdown won before session construction")
            attempt = next((item for item in self._open_attempts.values()
                            if item.task is asyncio.current_task() and item.configuration is not None), None)
            if attempt is None:
                raise RuntimeError("Session construction has no admitted open owner")
            session = KernelHostSession(
                model_runtime=self._model_runtime,
                agent_runtime=self._agent_runtime,
                root_session_id=root_session_id,
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
                image_validator=self._image_validator,
                local_mcp_configs=local_mcp_configs,
                mcp_configs=mcp_configs,
                scheduled_tasks=self.scheduled_tasks,
                media_cache=self._asr_cache,
                media_asr_runtime=self._media_asr_runtime,
            )
            attempt.candidate_session = session
            await session.start_mcp()
            await self._register_prepared_session(session, capability_baseline)
            return session
        except BaseException:
            if "session" in locals():
                with suppress(BaseException):
                    if self._sessions.get(host_id) is session:
                        await self.close_session(host_id)
                    else:
                        await self.close_prepared_session(session)
            else:
                with suppress(BaseException):
                    initial_plugin_view.close()
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
            if session.host_session_id in self._sessions:
                raise ConversationKernelConflict("IDENTITY_CONFLICT: Host identity already published")
            if session.root_session_id in self._suspended_agent_roots:
                raise RuntimeError("Owning ROOT Host is closing")
            self._sessions[session.host_session_id] = session
            if session.root_session_id is None:
                self._suspended_agent_roots.discard(session.session_id)
            self._extension_routes[session.session_id] = (
                session.host_session_id,
                session.extensions,
            )
            session.activate_execution()
            changed = self._workspace_capability_revisions[root] > capability_baseline
        session._renewal_task.add_done_callback(
            lambda task: self._close_unavailable_writer(session, task)
        )
        if changed:
            await session.request_capability_refresh()
        await self._agent_runtime.wake_agent_requests(session.root_session_id or session.session_id)

    def _close_unavailable_writer(
        self, session: KernelHostSession, renewal: asyncio.Task[None]
    ) -> None:
        if renewal.cancelled():
            return
        error = renewal.exception()
        if error is None or not session.writer_unavailable:
            return
        # The renewal owner is already done. This waiter requests the same
        # Core-owned close attempt that shutdown and recovery must join.
        closing = asyncio.create_task(
            self.close_session(session.host_session_id),
            name=f"kernel-writer-unavailable-close:{session.host_session_id}",
        )

        def observed(task: asyncio.Task[None]) -> None:
            if task.cancelled():
                return
            failure = task.exception()
            if failure is not None:
                logging.getLogger(__name__).error(
                    "unavailable Host writer physical close failed",
                    exc_info=(type(failure), failure, failure.__traceback__),
                )

        closing.add_done_callback(observed)


    async def begin_session_retirement(
        self,
        session_id: str,
        memory_domain_id: str,
    ) -> KernelSessionRetirement:
        async with self._lock:
            if self._closing or session_id in self._session_retirements:
                raise SessionDeleteRejected(
                    "SESSION_DELETE_BUSY", "会话正在处理另一项操作。"
                )
            pending = tuple(
                f
                for f, attempt in self._open_attempts.items()
                if session_id in attempt.session_ids
            )
            operation = KernelSessionRetirement(
                session_id,
                memory_domain_id,
                self,
                asyncio.get_running_loop().create_future(),
            )
            self._session_retirements[session_id] = operation
            self._open_attempts[operation.settlement] = KernelSessionOpenAttempt(
                frozenset((session_id,))
            )
        if pending:
            await asyncio.gather(*(asyncio.shield(f) for f in pending))
        return operation


    async def close_session(self, host_session_id: str) -> None:
        attempt = await self.request_close_session(host_session_id)
        if attempt is not None:
            # Cancellation detaches only this waiter from the installed owner.
            await asyncio.shield(attempt.task)

    def _install_session_close_locked(self, session: KernelHostSession) -> HostSessionCloseAttempt:
        attempt = self._close_attempts.get(session.host_session_id)
        if attempt is None:
            deadline = self._deadlines.deadline(KernelWatchdogOwner.HOST_SESSION_CLOSE)
            task = asyncio.create_task(self._close_session_owner(
                host_session_id=session.host_session_id, session=session, deadline_monotonic=deadline,
            ), name=f"kernel-host-close:{session.host_session_id}")
            attempt = HostSessionCloseAttempt(session.host_session_id, session, deadline, task)
            self._close_attempts[session.host_session_id] = attempt
        # The event-loop-local admission flag does not wait behind an already
        # accepted preview/database read holding the Session lock.
        session._closing = True
        return attempt

    async def request_close_session(self, host_session_id: str) -> HostSessionCloseAttempt | None:
        async with self._lock:
            attempt = self._close_attempts.get(host_session_id)
            if attempt is None:
                session = self._sessions.get(host_session_id)
                if session is None:
                    return None
                attempt = self._install_session_close_locked(session)
        return attempt

    async def close_prepared_session(self, session: KernelHostSession) -> None:
        """Keep a failed unpublished candidate in the existing close owner."""
        async with self._lock:
            if not any(attempt.candidate_session is session for attempt in self._open_attempts.values()):
                raise RuntimeError("prepared Session lost its opening owner")
            attempt = self._install_session_close_locked(session)
        await asyncio.shield(attempt.task)

    def session_runtime_closing(self, session_id: str) -> bool:
        return any(
            attempt.session.session_id == session_id
            and attempt.state in {HostSessionCloseState.TASK_INSTALLED,
                                  HostSessionCloseState.CLOSE_FAILED_QUARANTINED}
            for attempt in self._close_attempts.values()
        )

    @property
    def workbench_exit_deadline(self) -> float | None:
        deadlines = [attempt.deadline_monotonic for attempt in self._close_attempts.values()]
        deadlines.extend(self._resource_operations.values())
        deadlines.extend(attempt.io_owner.physical_operation_deadline
                         for attempt in self._open_attempts.values()
                         if attempt.io_owner is not None
                         and attempt.io_owner.physical_operation_deadline is not None)
        deadlines.extend(deadline for deadline in (
            self.scheduled_tasks.physical_operation_deadline,
            self._agent_runtime.physical_operation_deadline,
            self._history_maintenance.physical_operation_deadline if self._history_maintenance is not None else None,
            self._blob_gc_io.physical_operation_deadline if self._blob_gc_io is not None else None,
        ) if deadline is not None)
        initialization = self._resource_initialization
        if initialization is not None and (not initialization.cleanup_complete
                or (initialization.task is not None and not initialization.task.done())):
            deadlines.append(initialization.deadline_monotonic)
        return min(deadlines, default=None)

    @property
    def workbench_exit_ready(self) -> bool:
        deadline = self.workbench_exit_deadline
        settled_or_overdue = self._closed or (deadline is not None and monotonic() >= deadline)
        return (self._closing and settled_or_overdue
                and self._session_open_children_stopped()
                and self._image_validator.local_children_stopped
                and all(session.local_children_stopped for session in self._sessions.values())
                and all(attempt.session.local_children_stopped for attempt in self._close_attempts.values()))

    async def _close_session_owner(
        self,
        *,
        host_session_id: str,
        session: KernelHostSession,
        deadline_monotonic: float,
    ) -> None:
        close_error: BaseException | None = None
        if session.root_session_id is None:
            async with self._lock:
                self._suspended_agent_roots.add(session.session_id)
                owned = dict(self._sessions)
                owned.update((attempt.host_session_id, attempt.session) for attempt in self._close_attempts.values())
                children = tuple(child for child in owned.values()
                    if child.root_session_id == session.session_id)
            child_closes = tuple(asyncio.create_task(self.close_session(child.host_session_id)) for child in children)
            if children:
                terminal_stop = asyncio.create_task(session._tools.stop_terminal_physical_owners(
                    timeout_seconds=max(0.001, deadline_monotonic - monotonic())))
                await session._stop_close_admission()
                outcomes = await asyncio.gather(*child_closes, return_exceptions=True)
                try:
                    await terminal_stop
                except BaseException as error:
                    close_error = error
            else:
                outcomes = ()
            close_error = close_error or next((outcome for outcome in outcomes if isinstance(outcome, BaseException)), None)
            if any(not child.physical_close_complete for child in children):
                async with self._lock:
                    self._close_attempts[host_session_id].state = HostSessionCloseState.CLOSE_FAILED_QUARANTINED
                raise close_error or RuntimeError("Child Host physical close is unconfirmed")
        try:
            await session.aclose(
                deadline_monotonic=deadline_monotonic,
            )
        except BaseException as exc:
            close_error = close_error or exc
            if not session.physical_close_complete:
                async with self._lock:
                    attempt = self._close_attempts.get(host_session_id)
                    if attempt is not None:
                        attempt.state = HostSessionCloseState.CLOSE_FAILED_QUARANTINED
                raise
        if session.root_session_id is not None:
            await self._agent_runtime.release(session.session_id)
        # Physical completion is independent of a best-effort exact lease
        # release. A failure here never turns full close into false uncertainty.
        try:
            repository = self._repository
            if repository is None:
                raise RuntimeError("closed session lost its shared repository")
            await asyncio.to_thread(
                repository.release_host_writer, session._lease.guard,
                deadline_monotonic=self._canonical_deadline(),
            )
        except Exception:
            logging.getLogger(__name__).exception("clean Host writer release was not confirmed")
        async with self._lock:
            attempt = self._close_attempts.get(host_session_id)
            if attempt is not None:
                attempt.state = (HostSessionCloseState.CLOSED if session.necessary_settlement_complete
                                 else HostSessionCloseState.CLOSED_SETTLEMENT_UNCONFIRMED)
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
        if close_error is not None:
            if not session.necessary_settlement_complete or isinstance(close_error, KernelSessionSettlementUnconfirmed):
                raise KernelSessionSettlementUnconfirmed("Host canonical settlement is unconfirmed") from close_error
            logging.getLogger(__name__).warning("Closed Host diagnostic", exc_info=(type(close_error), close_error, close_error.__traceback__))

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
        close_error: BaseException | None = None

        async def close(operation) -> None:
            nonlocal close_error
            try:
                await operation()
            except BaseException as error:
                close_error = close_error or error
                logging.getLogger(__name__).warning(
                    "Core close diagnostic", exc_info=(type(error), error, error.__traceback__),
                )

        async with self._lock:
            owned_sessions = dict(self._sessions)
            owned_sessions.update((attempt.host_session_id, attempt.session) for attempt in self._close_attempts.values())
            roots = {session.session_id for session in owned_sessions.values() if session.root_session_id is None}
            session_ids = tuple(host_id for host_id, session in owned_sessions.items()
                                if session.root_session_id is None or session.root_session_id not in roots)
        # Install independent close owners before a shared IO join can block.
        # ROOT owners keep their existing child-before-parent dependency order.
        session_closes = tuple(asyncio.create_task(self.close_session(host_id),
                               name=f"kernel-shutdown-session:{host_id}") for host_id in session_ids)
        await close(self.scheduled_tasks.stop)
        await close(self._agent_runtime.aclose)
        await close(self._image_validator.aclose)
        if open_settlements:
            await asyncio.gather(*open_settlements, return_exceptions=True)
        operations = tuple(self._resource_operations)
        if operations:
            await asyncio.gather(*operations, return_exceptions=True)
        initialization = self._resource_initialization
        if initialization is not None and initialization.task is not None:
            await asyncio.gather(initialization.task, return_exceptions=True)

        outcomes = await asyncio.gather(
            *session_closes, return_exceptions=True,
        )
        session_error = next((result for result in outcomes if isinstance(result, BaseException)), None)
        close_error = close_error or session_error
        sessions_stopped = (not self._sessions and all(attempt.session.physical_close_complete
                            for attempt in self._close_attempts.values()))

        if self._history_maintenance is not None:
            owner = self._history_maintenance
            await close(lambda: owner.aclose(deadline_monotonic=self._canonical_deadline()))
            if owner.physical_close_complete:
                self._history_maintenance = None
        blob_deadline = self._deadlines.deadline(KernelWatchdogOwner.BLOB_GC_CLOSE)
        if self._blob_gc_task is not None:
            self._blob_gc_task.cancel()
            expired, cancellation, error = await _join_task_beyond_logical_deadline(
                self._blob_gc_task, deadline_monotonic=blob_deadline)
            if error is not None:
                logging.getLogger(__name__).warning(
                    "Blob GC exited with a close diagnostic",
                    exc_info=(type(error), error, error.__traceback__),
                )
            if expired:
                logging.getLogger(__name__).warning("Blob GC physically exited after close deadline")
            if cancellation is not None:
                logging.getLogger(__name__).warning("Blob GC close waiter was cancelled after physical join")
            self._blob_gc_task = None
        if self._blob_gc_io is not None:
            owner = self._blob_gc_io
            await close(lambda: owner.aclose(deadline_monotonic=blob_deadline))
            if owner.physical_close_complete:
                self._blob_gc_io = None

        # Unconfirmed physical work retains the resources it can still use.
        # This does not prevent independent maintenance owners from stopping.
        if sessions_stopped:
            if self._media_asr_runtime is not None:
                owner = self._media_asr_runtime
                await close(lambda: owner.aclose(deadline_monotonic=self._canonical_deadline()))
                if owner.physical_close_complete:
                    self._media_asr_runtime = None
            await close(self.scheduled_tasks.aclose)
            await close(self.mcp_management.aclose)
            await close(self._bundled_skill_binding.aclose)
            self._extension_routes.clear()
        initialization_clean = initialization is None or initialization.cleanup_complete
        resources_stopped = (sessions_stopped and initialization_clean
                             and self._agent_runtime.physical_close_complete
                             and self.scheduled_tasks.physical_close_complete
                             and self._image_validator.local_children_stopped
                             and self._media_asr_runtime is None
                             and self._history_maintenance is None and self._blob_gc_io is None)
        if resources_stopped:
            self._blob_store = None
            if self._access is not None:
                self._access.release()
                self._access = None
            self._repository = None
            self._asr_cache = None
            self._event_loop = None
            self._closed = True
        if not resources_stopped:
            raise close_error or RuntimeError("Kernel physical close is unconfirmed")
        if isinstance(session_error, KernelSessionSettlementUnconfirmed):
            raise session_error
        # Physically joined disposable maintenance and late-close diagnostics
        # are logged above. They do not turn an exited Core into quarantine.

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
        if any(event.event_type in {
            CommittedEventType.TURN_COMPLETED, CommittedEventType.TURN_INTERRUPTED,
            CommittedEventType.PROMPT_QUEUED, CommittedEventType.PROMPT_CONSUMED,
            CommittedEventType.PROMPT_CANCELLED, CommittedEventType.PROMPT_REJECTED,
        } for event in events):
            # Existing committed occurrences are wake hints only; subscribers
            # re-read canonical requests/turns instead of caching event state.
            asyncio.create_task(self._agent_runtime.notify_changed(),
                                name="kernel-agent-canonical-change")
        if self._history_maintenance is not None:
            entries: dict[str, list[str]] = {}
            for event in events:
                if event.subject.slot is SubjectSlot.ENTRY:
                    entries.setdefault(event.session_id, []).append(event.subject.subject_id)
            for session_id, entry_ids in entries.items():
                self._history_maintenance.offer_entries(session_id, tuple(entry_ids))
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


__all__ = [
    "KernelCapabilityCatalogInspection",
    "KernelCommandOutcome",
    "KernelCompositionUnavailable",
    "KernelHostCore",
    "KernelHostSession",
    "KernelMcpToolCatalogItem",
    "KernelSessionSummary",
]
