"""Production Host composition for the Stage 2 conversation kernel.

This composition deliberately has only canonical conversation, process-local
execution, selective journal, and exact-four background-job owners. Resume
acquires a new writer generation and rehydrates canonical rows only.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from time import monotonic
from typing import Awaitable, Callable
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
from pulsara_agent.conversation_kernel.activation import (
    require_stage2_runtime_privilege_boundary,
)
from pulsara_agent.conversation_kernel.blob import (
    CanonicalContentPublisher,
    PostgresCanonicalBlobStore,
)
from pulsara_agent.conversation_kernel.capability import KernelCapabilityComposer
from pulsara_agent.conversation_kernel.contracts import (
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
from pulsara_agent.conversation_kernel.jobs import KernelDurableJobExecutor
from pulsara_agent.conversation_kernel.interaction import KernelInteractionCoordinator
from pulsara_agent.conversation_kernel.plan_runtime import (
    ContinuationAdmissionOwner,
    KernelPlanInteractionCoordinator,
)
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.live_control import SessionLiveControlOwner
from pulsara_agent.conversation_kernel.memory_tools import KernelMemoryToolPort
from pulsara_agent.conversation_kernel.query import CanonicalConversationQuery
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
    plan_draft_review_semantic_candidate,
    plan_exit_semantic_fingerprint,
    plan_question_resolution_semantic_fingerprint,
)
from pulsara_agent.conversation_kernel.steer import (
    PromptIngressConfirmationKind,
    PromptIngressWriteRejection,
    build_prompt_ingress_command,
)
from pulsara_agent.conversation_kernel.runner import (
    ConversationKernelRunner,
    KernelRunResult,
)
from pulsara_agent.conversation_kernel.safe_point import ExternalSourceNotAtSafePoint
from pulsara_agent.conversation_kernel.subagent import KernelSubagentManager
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
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
from pulsara_agent.llm.models import ModelRole
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.tool_permission import (
    EffectivePermissionPolicy,
    default_permission_policy,
    mode_for_policy,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.plan_workflow import PlanDraftDecision, PlanHandoffKind
from pulsara_agent.ports.system_prompt import DEFAULT_SYSTEM_PROMPT
from pulsara_agent.ports.terminal_observation import (
    ExistingTurnInstallation,
    NewTurnInstallation,
)
from pulsara_agent.mcp_config import McpServerConfig, load_mcp_server_configs
from pulsara_agent.conversation_kernel.mcp import McpHostSupervisor
from pulsara_agent.settings import PulsaraSettings
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.storage.schema_verification_service import (
    VerifiedPostgresAccessLease,
    process_postgres_schema_verification_service,
)


MAXIMUM_PROMPT_BYTES = STAGE2_LIMITS.prompt_hard_bytes


@dataclass(frozen=True, slots=True)
class KernelSessionSummary:
    session_id: str
    workspace_id: str
    lifecycle: str
    writer_generation: int
    latest_entry_sequence: int
    updated_at: datetime

    @property
    def runtime_session_id(self) -> str:
        return self.session_id

    def to_dict(self) -> dict[str, object]:
        return {
            "runtime_session_id": self.session_id,
            "workspace_id": self.workspace_id,
            "lifecycle": self.lifecycle,
            "writer_generation": self.writer_generation,
            "latest_entry_sequence": self.latest_entry_sequence,
            "updated_at": self.updated_at.isoformat(),
        }


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


class KernelCompositionUnavailable(ValueError):
    pass


class HostSessionCloseState(StrEnum):
    TASK_INSTALLED = "TASK_INSTALLED"
    CLOSED = "CLOSED"
    CLOSE_FAILED_QUARANTINED = "CLOSE_FAILED_QUARANTINED"


class HostSessionCloseDecisionFrozen(RuntimeError):
    """A canonical-close upgrade arrived after its linearization fence."""


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
        settings: PulsaraSettings,
        workspace: ResolvedWorkspace,
        repository: ConversationKernelRepository,
        writer_lease: WriterLease,
        io_owner: KernelSessionIO,
        session_id: str,
        host_session_id: str,
        permission_policy: EffectivePermissionPolicy,
        model_role: ModelRole,
        system_prompt: str | None,
        active_skill_names: frozenset[str],
        authenticated_first_party_extension_ids: frozenset[str],
        deadline_factory: KernelExecutionDeadlineFactory,
        mcp_configs: tuple[McpServerConfig, ...] = (),
    ) -> None:
        self.settings = settings
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
        self._lease = writer_lease
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
        self._plan_interactions = KernelPlanInteractionCoordinator()
        self._plan_continuations = ContinuationAdmissionOwner()
        self._input_continuity = HostProviderInputContinuityOwner(session_id=session_id)
        self._event_loop = asyncio.get_running_loop()
        launch_permission_mode = mode_for_policy(permission_policy)
        if launch_permission_mode is None:
            raise KernelCompositionUnavailable(
                "production Host permission must be a closed preset"
            )
        self._launch_permission_mode = launch_permission_mode
        self._monitor_wake = asyncio.Event()

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
        )
        self._tools.bind_interaction_port(self._interactions)
        self._subagents = KernelSubagentManager(
            repository=repository,
            guard=self._lease.guard,
            host_owner_id=host_session_id,
            io_owner=self._io,
            live_bus=self.live_bus,
            deadline_factory=self._deadlines,
        )
        self._tools.bind_subagent_port(self._subagents)
        self._memory_tools = KernelMemoryToolPort(
            repository=repository,
            workspace_id=workspace.workspace_key,
            embedding_config=settings.retrieval.embedding,
            io_owner=self._io,
        )
        self._tools.bind_memory_port(self._memory_tools)
        self._mcp_supervisor = McpHostSupervisor(
            session_id=session_id,
            workspace_root=workspace.workspace_root,
            configs=mcp_configs,
        )
        self._tools.bind_mcp_supervisor(self._mcp_supervisor)
        self._capabilities = KernelCapabilityComposer(
            workspace_root=workspace.workspace_root,
            workspace_kind=workspace.workspace_kind,
            memory_domain=workspace.memory_domain,
            available_tool_names=frozenset(
                spec.name for spec in self._tools.tool_specs
            ),
            configured_active_skill_names=active_skill_names,
        )
        self._model = DirectKernelModelPort(
            config=settings.llm,
            role=model_role,
            usage_observer=self._observe_provider_usage,
            timeout_policy=self._deadlines.policy.foreground_transport,
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
        )
        self._runner = ConversationKernelRunner(
            repository=repository,
            writer_lease=self._lease,
            model=self._model,
            tools=self._tools,
            live_bus=self.live_bus,
            io_owner=self._io,
            context_source_collector=self._context_sources,
            continuity_owner=self._input_continuity,
            extensions=self.extensions,
            workspace_id=workspace.workspace_key,
            plan_interactions=self._plan_interactions,
            automatic_plan_continuation=(self._accept_automatic_plan_continuation),
            deadline_factory=self._deadlines,
        )
        self._subagents.bind_runner_factory(self._new_child_runner)
        self._active_task: asyncio.Task[KernelRunResult] | None = None
        self._active_turn_id: str | None = None
        self._active_command_id: str | None = None
        self._external_new_turn_accepting = False
        self._plan_exit_fence = False
        self._terminal_new_turn_observation_id: str | None = None
        self._external_new_turn_settled = asyncio.Event()
        self._external_new_turn_settled.set()
        self._command_failures: dict[str, KernelCommandOutcome] = {}
        self._lock = asyncio.Lock()
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
        self._monitor_task = asyncio.create_task(
            self._terminal_monitor_delivery_loop(),
            name=f"kernel-terminal-monitor-delivery:{session_id}",
        )
        self._queue_wake.set()

    async def start_mcp(self) -> None:
        await self._mcp_supervisor.start()
        self._tools.prepare_tool_surface_safe_point()

    async def reload_mcp_configs(
        self, configs: tuple[McpServerConfig, ...]
    ) -> frozenset[str]:
        """Install a process-local config epoch; publication waits for safe point."""

        self._require_open()
        return await self._tools.reload_mcp_configs(configs)

    def reconnect_mcp_server(self, server_id: str) -> None:
        """Request a fresh physical generation for future safe-point borrows."""

        self._require_open()
        self._mcp_supervisor.reconnect(server_id)

    @property
    def writer_generation(self) -> int:
        return self._lease.guard.writer_generation

    @property
    def active_skill_names(self) -> frozenset[str]:
        return self._capabilities.configured_active_skill_names

    def _canonical_deadline(self) -> float:
        """Issue a fresh watchdog for one foreground canonical operation."""

        return self._deadlines.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL)

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
        turn_id = _stable_id("turn", self.session_id, command)
        existing = await self._query_command_row(command)
        if existing is not None:
            raise RuntimeError("command was already accepted; query its outcome")
        async with self._lock:
            self._require_open()
            self._retire_done_active_root_locked()
            if (
                self._plan_exit_fence
                or self._external_new_turn_accepting
                or self._active_task is not None
            ):
                raise RuntimeError("a canonical ROOT turn is already running")
            task = self._install_active_root_task_locked(
                turn_id=turn_id,
                command_id=command,
                name=f"kernel-turn:{command}",
                run=lambda: self._run_root_turn_chain(
                    text,
                    command_id=command,
                    requested_permission_mode=(
                        requested_permission_mode or self._launch_permission_mode
                    ),
                ),
            )
        # The Host owns the run chain.  A gateway/request cancellation detaches
        # only this waiter; the task itself settles the ROOT slot when its full
        # continuation lineage physically exits.
        return await asyncio.shield(task)

    async def _run_root_turn_chain(
        self,
        text: str,
        *,
        command_id: str,
        requested_permission_mode: PermissionMode,
    ) -> KernelRunResult:
        result = await self._runner.run_turn(
            text,
            command_id=command_id,
            requested_permission_mode=requested_permission_mode,
        )
        return await self._finish_root_chain(result)

    async def _run_accepted_root_chain(
        self,
        turn_id: str,
    ) -> KernelRunResult:
        """Run one accepted ROOT turn and every automatic successor."""

        result = await self._runner.run_accepted_turn(turn_id)
        return await self._finish_root_chain(result)

    async def _finish_root_chain(self, result: KernelRunResult) -> KernelRunResult:
        total_model_calls = result.model_call_count
        total_tool_calls = result.tool_call_count
        while result.continuation_turn_id is not None:
            continuation_turn_id = result.continuation_turn_id
            async with self._lock:
                if self._active_task is not asyncio.current_task():
                    raise RuntimeError("ROOT continuation lost its Host task owner")
                self._active_turn_id = continuation_turn_id
            result = await self._runner.run_accepted_turn(continuation_turn_id)
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
        run: Callable[[], Awaitable[KernelRunResult]],
    ) -> asyncio.Task[KernelRunResult]:
        """Install the sole Host-owned ROOT task while ``self._lock`` is held."""

        self._retire_done_active_root_locked()
        if self._active_task is not None:
            raise RuntimeError("a canonical ROOT turn is already running")
        task = asyncio.create_task(
            self._run_owned_root_task(run),
            name=name,
        )
        self._active_task = task
        self._active_turn_id = turn_id
        self._active_command_id = command_id
        task.add_done_callback(self._observe_active_root_task_done)
        return task

    @staticmethod
    def _observe_active_root_task_done(task: asyncio.Task[KernelRunResult]) -> None:
        """Consume a detached ROOT task exception after task-owned settlement."""

        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass

    async def _run_owned_root_task(
        self,
        run: Callable[[], Awaitable[KernelRunResult]],
    ) -> KernelRunResult:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("ROOT task has no asyncio owner")
        try:
            return await run()
        finally:
            await self._settle_active_root_task(task)

    async def _settle_active_root_task(self, task: asyncio.Task[object]) -> None:
        async with self._lock:
            if self._active_task is task:
                self._clear_active_root_locked()

    def _retire_done_active_root_locked(self) -> None:
        task = self._active_task
        if task is not None and task.done():
            self._clear_active_root_locked()

    def _clear_active_root_locked(self) -> None:
        self._active_task = None
        self._active_turn_id = None
        self._active_command_id = None
        self._queue_wake.set()
        self._monitor_wake.set()

    def _admit_plan_resolution_write_locked(self, *, creates_turn: bool) -> None:
        """Linearize every new Plan-resolution write against force exit."""

        self._require_open()
        if self._plan_exit_fence:
            raise ConversationKernelConflict(
                "Plan resolution conflicts with force exit"
            )
        if not creates_turn:
            return
        self._retire_done_active_root_locked()
        if self._external_new_turn_accepting or self._active_task is not None:
            raise ConversationKernelConflict("a canonical ROOT turn is already running")
        self._external_new_turn_accepting = True
        self._external_new_turn_settled.clear()

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
                        # The same Host-owned run-chain task is rebound before
                        # the origin runner is released.
                        self._active_turn_id = candidate.continuation_turn_id
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
            existing = await self.query_command(command_id)
            if existing is None:
                raise RuntimeError("compatible prompt command has no canonical outcome")
            return existing
        async with self._lock:
            if self._plan_exit_fence:
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    "",
                    "PLAN_TRANSITION_BUSY",
                    "A Plan force-exit transition is in progress.",
                )
        content = await self._io.run(
            self._content_publisher.materialize,
            session_id=self.session_id,
            content=content_utf8,
            media_type="text/plain",
            codec="utf-8",
            deadline_monotonic=self._canonical_deadline(),
        )
        try:
            queue_sequence = await self._io.run(
                self.repository.enqueue_prompt,
                self._lease.guard,
                command_id=command_id,
                queue_item_id=queue_item_id,
                client_submission_id=command_id,
                delivery_mode=delivery_mode,
                target_turn_id=target_turn_id,
                permission_snapshot_id=permission_snapshot_id,
                requested_permission_mode=effective_requested_permission,
                content=content,
                occurred_at=datetime.now().astimezone(),
                actor_id=self.host_session_id,
                deadline_monotonic=self._canonical_deadline(),
            )
        except PromptIngressRejected as exc:
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
                self._queue_wake.set()
                existing = await self.query_command(command_id)
                if existing is None:
                    raise RuntimeError(
                        "compatible prompt command has no canonical outcome"
                    )
                return existing
            if confirmation.kind is PromptIngressConfirmationKind.CONFLICT:
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    queue_item_id,
                    "COMMAND_CONFLICT",
                    "The command identity names a different prompt.",
                )
            raise
        self._queue_wake.set()
        return KernelCommandOutcome(
            command_id,
            "PENDING",
            queue_item_id,
            "PROMPT_QUEUED",
            f"Prompt accepted at queue sequence {queue_sequence}.",
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
            if self._plan_exit_fence or self._external_new_turn_accepting:
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

        async def resolve() -> AcceptedPlanResolution:
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

        async with self._lock:
            attempt = self._plan_continuations.start(
                attempt_id=f"plan-question-resolution:{command_id}",
                turn_id=f"question:{interaction_id}",
                semantic_candidate_fingerprint=semantic_candidate_fingerprint,
                run=resolve,
                before_start=lambda: self._admit_plan_resolution_write_locked(
                    creates_turn=False
                ),
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

        async with self._lock:

            def reserve_new_attempt() -> None:
                nonlocal reserved
                self._admit_plan_resolution_write_locked(creates_turn=creates_turn)
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
            self._install_active_root_task_locked(
                turn_id=inspection.turn_id,
                command_id=command_id,
                name=f"kernel-plan-review-turn:{inspection.turn_id}",
                run=lambda: self._run_accepted_root_chain(inspection.turn_id),
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
        delay_seconds = 0.05
        while True:
            try:
                await self._io.run(
                    self.repository.interrupt_turn,
                    self._lease.guard,
                    turn_id=turn_id,
                    reason="PLAN_CONTINUATION_NOT_BOUND",
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
                    accepted = await self._io.run(
                        self.repository.consume_prompt_head,
                        self._lease.guard,
                        new_turn_id=f"turn:{uuid4().hex}",
                        new_entry_id=f"entry:{uuid4().hex}",
                        new_context_binding_revision_id=(
                            f"context-revision:{uuid4().hex}"
                        ),
                        occurred_at=datetime.now().astimezone(),
                        actor_id=self.host_session_id,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # The queue row is the level truth.  A transient checkout
                    # or writer failure must not terminate the only process-
                    # local delivery loop or create a durable retry owner.
                    await asyncio.sleep(0.1)
                    self._queue_wake.set()
                    break
                if accepted is None:
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
                async with self._lock:
                    task = self._install_active_root_task_locked(
                        turn_id=accepted.turn_id,
                        command_id=None,
                        name=f"kernel-queued-turn:{accepted.turn_id}",
                        run=lambda: self._run_accepted_root_chain(accepted.turn_id),
                    )
                    if self._closing:
                        task.cancel()
                try:
                    await task
                except BaseException:
                    pass
                finally:
                    # Covers cancellation before the task wrapper gets its
                    # first scheduling turn; the normal path already settled
                    # itself in ``_run_owned_root_task``.
                    await self._settle_active_root_task(task)

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
                monitor_id = monitor_ids[0]
                observation_id = coordinator.pending_observation_id(monitor_id)
                if observation_id is None:
                    continue
                attempt = coordinator.current_installation_attempt(monitor_id)
                target = None if attempt is None else attempt.target
                reserved_new_turn = False
                if target is None:
                    async with self._lock:
                        self._retire_done_active_root_locked()
                        active = self._active_task
                        active_turn_id = self._active_turn_id
                        if active is not None and active_turn_id is not None:
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
                            reserved_new_turn = True
                        elif (
                            self._active_task is None
                            and not self._external_new_turn_accepting
                        ):
                            self._external_new_turn_accepting = True
                            self._terminal_new_turn_observation_id = observation_id
                            self._external_new_turn_settled.clear()
                            reserved_new_turn = True
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
                    raise
                except (ConversationKernelConflict, ExternalSourceNotAtSafePoint):
                    if reserved_new_turn:
                        await self._release_terminal_new_turn_reservation(
                            observation_id
                        )
                    await asyncio.sleep(0.05)
                    self._monitor_wake.set()
                    break
                except Exception:
                    # The immutable attempt remains process-local and is
                    # exact-confirmed by the next pass before any re-write.
                    await asyncio.sleep(0.1)
                    self._monitor_wake.set()
                    break
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
            self._install_active_root_task_locked(
                turn_id=turn_id,
                command_id=None,
                name=f"kernel-terminal-observation-turn:{turn_id}",
                run=lambda: self._run_accepted_root_chain(turn_id),
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
        if row.get("command_kind") == "ACCEPT_SUBAGENT_RESULT":
            return KernelCommandOutcome(
                command_id,
                "SUCCEEDED",
                str(row.get("target_entry_id") or ""),
                "SUBAGENT_RESULT_ACCEPTED",
                "The durable child result was accepted into the ROOT conversation.",
            )
        if row.get("command_kind") == "ACCEPT_JOB_RESULT":
            return KernelCommandOutcome(
                command_id,
                "SUCCEEDED",
                str(row.get("target_entry_id") or ""),
                "JOB_RESULT_ACCEPTED",
                "The durable job result was accepted into the ROOT conversation.",
            )
        status = str(row.get("turn_status") or "")
        target = str(row.get("target_turn_id") or "")
        if row.get("target_queue_item_id") is not None:
            queue_status = str(row.get("queue_status") or "")
            target = str(row.get("consumed_turn_id") or row["target_queue_item_id"])
            status = str(row.get("consumed_turn_status") or "")
            if queue_status == "PENDING":
                return KernelCommandOutcome(
                    command_id, "PENDING", target, "PROMPT_QUEUED", "Prompt is queued."
                )
            if queue_status in {"CANCELLED", "REJECTED"}:
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    target,
                    str(row.get("queue_terminal_reason") or queue_status),
                    "The queued prompt was not delivered.",
                )
            if queue_status == "CONSUMED" and not status:
                return KernelCommandOutcome(
                    command_id,
                    "PENDING",
                    target,
                    "PROMPT_CONSUMED",
                    "Prompt was accepted into a canonical turn.",
                )
        if status == "COMPLETED":
            return KernelCommandOutcome(
                command_id, "SUCCEEDED", target, "TURN_COMPLETED", "Reply accepted."
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
            command_id, "PENDING", target, "TURN_RUNNING", "The turn is running."
        )

    def attach_controller(self, attachment_id: str) -> bool:
        self._require_open()
        return self._interactions.attach_controller(attachment_id)

    def has_controller_attachment(self, attachment_id: str) -> bool:
        """Validate the process-local Plan-content capability holder."""

        return self._interactions.is_current_controller(attachment_id)

    async def controller_detached(self, attachment_id: str) -> None:
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

    async def stop_current_turn(self) -> bool:
        async with self._lock:
            task = self._active_task
        if task is None or task.done():
            return False
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        await self._settle_active_root_task(task)
        return True

    async def accept_subagent_result(
        self,
        *,
        command_id: str,
        target_turn_id: str | None,
        child_result_id: str,
        actor_id: str,
    ) -> KernelCommandOutcome:
        self._require_open()
        existing = await self.query_command(command_id)
        if existing is not None:
            return existing
        new_turn = target_turn_id is None
        resolved_turn_id = target_turn_id or _stable_id(
            "turn", self.session_id, command_id
        )
        new_revision_id = (
            _stable_id("context-revision", resolved_turn_id, "0") if new_turn else None
        )
        if new_turn and not await self._reserve_external_new_turn():
            return KernelCommandOutcome(
                command_id,
                "REJECTED",
                child_result_id,
                "ROOT_TURN_ALREADY_RUNNING",
                "A ROOT turn is already running.",
            )
        try:
            accepted = await self._runner.accept_subagent_result(
                turn_id=resolved_turn_id,
                new_context_binding_revision_id=new_revision_id,
                child_result_id=child_result_id,
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
                child_result_id,
                "PROVIDER_SAFE_POINT_REQUIRED",
                "The ROOT turn is currently dispatching a provider call.",
            )
        except BaseException as error:
            confirmed = await self._confirm_external_result_command(
                command_id=command_id,
                command_kind="ACCEPT_SUBAGENT_RESULT",
                source_id=child_result_id,
                expected_turn_id=resolved_turn_id,
            )
            if confirmed is not None:
                if new_turn:
                    await self._start_external_result_turn(resolved_turn_id, command_id)
                if isinstance(error, asyncio.CancelledError):
                    raise
                return confirmed
            if new_turn:
                await self._release_external_new_turn_reservation()
            raise
        if accepted is None:
            if new_turn:
                await self._release_external_new_turn_reservation()
            return KernelCommandOutcome(
                command_id,
                "REJECTED",
                child_result_id,
                "SUBAGENT_RESULT_UNAVAILABLE",
                "The durable child result cannot be accepted into this turn.",
            )
        if new_turn:
            await self._start_external_result_turn(accepted.turn_id, command_id)
        return KernelCommandOutcome(
            command_id,
            "SUCCEEDED",
            accepted.entry_id,
            "SUBAGENT_RESULT_ACCEPTED",
            "The durable child result was accepted into the ROOT conversation.",
        )

    async def accept_job_result(
        self,
        *,
        command_id: str,
        target_turn_id: str | None,
        job_id: str,
        actor_id: str,
    ) -> KernelCommandOutcome:
        self._require_open()
        existing = await self.query_command(command_id)
        if existing is not None:
            return existing
        new_turn = target_turn_id is None
        resolved_turn_id = target_turn_id or _stable_id(
            "turn", self.session_id, command_id
        )
        new_revision_id = (
            _stable_id("context-revision", resolved_turn_id, "0") if new_turn else None
        )
        if new_turn and not await self._reserve_external_new_turn():
            return KernelCommandOutcome(
                command_id,
                "REJECTED",
                job_id,
                "ROOT_TURN_ALREADY_RUNNING",
                "A ROOT turn is already running.",
            )
        try:
            accepted = await self._runner.accept_job_result(
                turn_id=resolved_turn_id,
                new_context_binding_revision_id=new_revision_id,
                job_id=job_id,
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
                job_id,
                "PROVIDER_SAFE_POINT_REQUIRED",
                "The ROOT turn is currently dispatching a provider call.",
            )
        except BaseException as error:
            confirmed = await self._confirm_external_result_command(
                command_id=command_id,
                command_kind="ACCEPT_JOB_RESULT",
                source_id=job_id,
                expected_turn_id=resolved_turn_id,
            )
            if confirmed is not None:
                if new_turn:
                    await self._start_external_result_turn(resolved_turn_id, command_id)
                if isinstance(error, asyncio.CancelledError):
                    raise
                return confirmed
            if new_turn:
                await self._release_external_new_turn_reservation()
            raise
        if accepted is None:
            if new_turn:
                await self._release_external_new_turn_reservation()
            return KernelCommandOutcome(
                command_id,
                "REJECTED",
                job_id,
                "JOB_RESULT_UNAVAILABLE",
                "The durable job result cannot be accepted into this turn.",
            )
        if new_turn:
            await self._start_external_result_turn(accepted.turn_id, command_id)
        return KernelCommandOutcome(
            command_id,
            "SUCCEEDED",
            accepted.entry_id,
            "JOB_RESULT_ACCEPTED",
            "The durable job result was accepted into the ROOT conversation.",
        )

    async def _reserve_external_new_turn(self) -> bool:
        async with self._lock:
            self._retire_done_active_root_locked()
            if (
                self._closing
                or self._plan_exit_fence
                or self._external_new_turn_accepting
                or self._active_task is not None
            ):
                return False
            self._external_new_turn_accepting = True
            self._external_new_turn_settled.clear()
            return True

    async def _confirm_external_result_command(
        self,
        *,
        command_id: str,
        command_kind: str,
        source_id: str,
        expected_turn_id: str,
    ) -> KernelCommandOutcome | None:
        try:
            row = await self._query_command_row(command_id)
        except BaseException:
            return None
        if row is None or row.get("command_kind") != command_kind:
            return None
        observed_source = (
            row.get("target_entry_source_subagent_result_id")
            if command_kind == "ACCEPT_SUBAGENT_RESULT"
            else row.get("target_entry_source_job_id")
        )
        if (
            observed_source != source_id
            or row.get("target_entry_turn_id") != expected_turn_id
            or not row.get("target_entry_id")
        ):
            return None
        return KernelCommandOutcome(
            command_id,
            "SUCCEEDED",
            str(row["target_entry_id"]),
            (
                "SUBAGENT_RESULT_ACCEPTED"
                if command_kind == "ACCEPT_SUBAGENT_RESULT"
                else "JOB_RESULT_ACCEPTED"
            ),
            "The durable external result was accepted into the ROOT conversation.",
        )

    async def _release_external_new_turn_reservation(self) -> None:
        async with self._lock:
            self._external_new_turn_accepting = False
            self._external_new_turn_settled.set()
            self._queue_wake.set()

    async def _start_external_result_turn(self, turn_id: str, command_id: str) -> None:
        async with self._lock:
            self._retire_done_active_root_locked()
            if not self._external_new_turn_accepting or self._active_task is not None:
                self._external_new_turn_accepting = False
                self._external_new_turn_settled.set()
                raise RuntimeError("external result turn lost its local admission")
            self._install_active_root_task_locked(
                turn_id=turn_id,
                command_id=command_id,
                name=f"kernel-external-result-turn:{turn_id}",
                run=lambda: self._run_accepted_root_chain(turn_id),
            )
            self._external_new_turn_accepting = False
            self._external_new_turn_settled.set()

    async def request_job_cancel(
        self,
        *,
        job_id: str,
        reason: str = "USER_CANCELLED",
    ) -> str:
        self._require_open()
        return await self._io.run(
            self.repository.request_job_cancel,
            self._lease.guard,
            job_id=job_id,
            actor_id=self.host_session_id,
            reason=reason,
            deadline_monotonic=self._deadlines.deadline(
                KernelWatchdogOwner.FOREGROUND_CANONICAL
            ),
        )

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
            async with self._lock:
                self._closing = True
                self._close_conversation_requested = (
                    self._close_conversation_requested or close_conversation
                )
                self._queue_wake.set()
                self._monitor_wake.set()
            self.extensions.stop_admission()
            self._mcp_supervisor.stop_admission()
            mcp_close_task = asyncio.create_task(
                self._mcp_supervisor.aclose(),
                name=f"kernel-mcp-close:{self.host_session_id}",
            )
            deadline = (
                self._deadlines.deadline(KernelWatchdogOwner.HOST_SESSION_CLOSE)
                if deadline_monotonic is None
                else deadline_monotonic
            )
            close_error: BaseException | None = None
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
            self._renewal_task.cancel()
            try:
                await self._renewal_task
            except BaseException:
                pass
            self._delivery_task.cancel()
            try:
                await self._delivery_task
            except BaseException:
                pass
            for close_operation in (
                self._plan_interactions.aclose,
                self._plan_continuations.aclose,
                self._interactions.aclose,
            ):
                try:
                    await close_operation()
                except BaseException as exc:
                    close_error = close_error or exc
            try:
                await self._subagents.aclose(
                    timeout_seconds=max(0.001, deadline - monotonic())
                )
            except BaseException as exc:
                close_error = close_error or exc
            self._input_continuity.close()
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
                if await _join_close_task(
                    mcp_close_task, deadline_monotonic=deadline
                ):
                    close_error = close_error or TimeoutError(
                        "MCP physical owners exited after close deadline"
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
                deadline_monotonic=self._deadlines.deadline(
                    KernelWatchdogOwner.WRITER_RENEWAL
                ),
            )

    def _new_child_runner(self) -> ConversationKernelRunner:
        return ConversationKernelRunner(
            repository=self.repository,
            writer_lease=self._lease,
            model=self._model,
            tools=self._tools,
            live_bus=self.live_bus,
            io_owner=self._io,
            context_source_collector=self._context_sources,
            continuity_owner=self._input_continuity,
            extensions=self.extensions,
            workspace_id=self.workspace.workspace_key,
            launch_permission_mode=self._launch_permission_mode,
            deadline_factory=self._deadlines,
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
            SELECT id, workspace_id, lifecycle, writer_generation,
                   latest_entry_sequence, updated_at
            FROM pulsara_v3.sessions
            WHERE workspace_id = %s AND (%s OR lifecycle = 'OPEN')
            ORDER BY updated_at DESC, id LIMIT %s
            """,
            (workspace_id, include_closed, limit),
        ).fetchall()


class KernelHostCore:
    """Process owner for verified PostgreSQL and live Host sessions."""

    def __init__(
        self,
        *,
        settings: PulsaraSettings,
        authenticated_first_party_extension_ids: frozenset[str] = frozenset(),
        watchdog_policy: KernelExecutionWatchdogPolicy | None = None,
    ) -> None:
        self.settings = settings
        self._deadlines = KernelExecutionDeadlineFactory(
            watchdog_policy or DEFAULT_KERNEL_WATCHDOG_POLICY
        )
        self._access: VerifiedPostgresAccessLease | None = None
        self._repository: ConversationKernelRepository | None = None
        self._jobs: KernelDurableJobExecutor | None = None
        self._blob_store: PostgresCanonicalBlobStore | None = None
        self._blob_gc_io: KernelSessionIO | None = None
        self._blob_gc_task: asyncio.Task[None] | None = None
        self._sessions: dict[str, KernelHostSession] = {}
        self._close_attempts: dict[str, HostSessionCloseAttempt] = {}
        self._extension_routes: dict[str, tuple[str, KernelExtensionHost]] = {}
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()
        self._authenticated_first_party_extension_ids = (
            authenticated_first_party_extension_ids
        )

    def _canonical_deadline(self) -> float:
        return self._deadlines.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL)

    @classmethod
    def production(
        cls,
        *,
        settings: PulsaraSettings,
        authenticated_first_party_extension_ids: frozenset[str] = frozenset(),
        watchdog_policy: KernelExecutionWatchdogPolicy | None = None,
    ) -> "KernelHostCore":
        return cls(
            settings=settings,
            authenticated_first_party_extension_ids=(
                authenticated_first_party_extension_ids
            ),
            watchdog_policy=watchdog_policy,
        )

    async def _ensure_resources(self) -> ConversationKernelRepository:
        if self._repository is None:
            self._event_loop = asyncio.get_running_loop()
            self._access = await process_postgres_schema_verification_service().acquire(
                self.settings.storage.postgres_dsn,
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
            self._jobs = KernelDurableJobExecutor(
                repository=self._repository,
                llm_config=self.settings.llm,
                embedding_config=self.settings.retrieval.embedding,
                deadline_factory=self._deadlines,
            )
            self._jobs.start()
            self._blob_store = PostgresCanonicalBlobStore(
                self._access.connection_provider
            )
            self._blob_gc_io = KernelSessionIO(maximum_concurrency=1)
            self._blob_gc_task = asyncio.create_task(
                self._blob_gc_loop(), name="kernel-blob-orphan-gc"
            )
        return self._repository

    async def open_session(
        self,
        workspace_input: HostWorkspaceInput,
        *,
        model_role: ModelRole = ModelRole.PRO,
        permission_policy: EffectivePermissionPolicy | None = None,
        system_prompt: str | None = None,
        active_skill_names: frozenset[str] = frozenset(),
    ) -> KernelHostSession:
        return await self._open(
            workspace_input,
            session_id=f"session:{uuid4().hex}",
            model_role=model_role,
            permission_policy=permission_policy,
            system_prompt=system_prompt,
            active_skill_names=active_skill_names,
        )

    async def resume_session(
        self,
        session_id: str,
        *,
        workspace_input: HostWorkspaceInput,
        model_role: ModelRole = ModelRole.PRO,
        permission_policy: EffectivePermissionPolicy | None = None,
        system_prompt: str | None = None,
        active_skill_names: frozenset[str] = frozenset(),
    ) -> KernelHostSession:
        return await self._open(
            workspace_input,
            session_id=session_id,
            model_role=model_role,
            permission_policy=permission_policy,
            system_prompt=system_prompt,
            active_skill_names=active_skill_names,
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
        model_role: ModelRole,
        permission_policy: EffectivePermissionPolicy | None,
        system_prompt: str | None,
        active_skill_names: frozenset[str],
    ) -> KernelHostSession:
        workspace = resolve_workspace(workspace_input)
        mcp_configs = await asyncio.to_thread(
            load_mcp_server_configs,
            workspace_root=workspace.workspace_root,
            trust_workspace_config=workspace.trust_workspace_mcp_config,
        )
        repository = await self._ensure_resources()
        host_id = f"host:{uuid4().hex}"
        io_owner = KernelSessionIO()
        deadline = self._deadlines.deadline(
            KernelWatchdogOwner.FOREGROUND_CANONICAL
        )
        try:
            writer_lease = await io_owner.run(
                repository.acquire_host_writer,
                session_id=session_id,
                workspace_id=workspace.workspace_key,
                writer_owner_id=host_id,
                lease_seconds=self._deadlines.policy.writer_lease_seconds,
                deadline_monotonic=deadline,
            )
            session = KernelHostSession(
                settings=self.settings,
                workspace=workspace,
                repository=repository,
                writer_lease=writer_lease,
                io_owner=io_owner,
                session_id=session_id,
                host_session_id=host_id,
                permission_policy=permission_policy or default_permission_policy(),
                model_role=model_role,
                system_prompt=system_prompt,
                active_skill_names=active_skill_names,
                authenticated_first_party_extension_ids=(
                    self._authenticated_first_party_extension_ids
                ),
                deadline_factory=self._deadlines,
                mcp_configs=mcp_configs,
            )
            await session.start_mcp()
        except BaseException:
            if "session" in locals():
                with suppress(BaseException):
                    await session.aclose(deadline_monotonic=deadline)
            await io_owner.aclose(deadline_monotonic=deadline)
            raise
        async with self._lock:
            self._sessions[host_id] = session
            self._extension_routes[session_id] = (host_id, session.extensions)
        return session

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
            include_closed,
            limit,
            self._canonical_deadline(),
        )
        return [
            KernelSessionSummary(
                session_id=str(row["id"]),
                workspace_id=str(row["workspace_id"]),
                lifecycle=str(row["lifecycle"]),
                writer_generation=int(row["writer_generation"]),
                latest_entry_sequence=int(row["latest_entry_sequence"]),
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

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
            session_ids = tuple(self._sessions)
        for host_session_id in session_ids:
            await self.close_session(host_session_id, close_conversation=False)
        self._extension_routes.clear()
        if self._jobs is not None:
            await self._jobs.aclose(
                deadline_monotonic=self._deadlines.deadline(
                    KernelWatchdogOwner.DURABLE_JOB_EXECUTOR_CLOSE
                )
            )
            self._jobs = None
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
                self._blob_gc_io.aclose(
                    deadline_monotonic=blob_close_deadline
                ),
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
    "KernelCommandOutcome",
    "KernelCompositionUnavailable",
    "KernelHostCore",
    "KernelHostSession",
    "KernelSessionSummary",
    "HostSessionCloseDecisionFrozen",
]
