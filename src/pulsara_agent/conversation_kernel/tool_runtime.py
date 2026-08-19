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

from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry
from pulsara_agent.model_input.contracts import (
    FrozenModelToolSurface,
    FrozenToolSpec,
    ModelInputScopeKind,
    STRUCTURED_MODEL_INPUT_LIMITS,
    model_tool_surface_fingerprint,
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
from pulsara_agent.terminal_process import (
    TerminalProcessInfo,
    TerminalProcessOrigin,
    TerminalRequest,
    TerminalResult,
    TerminalSessionManager,
)
from pulsara_agent.terminal_process.output import TerminalOutputSnapshot
from pulsara_agent.terminal_process.monitor import (
    PreparedTerminalMonitorRegistration,
    TerminalMonitorCoordinator,
    TerminalMonitorPolicy,
    TerminalMonitorRejected,
)
from pulsara_agent.tools.builtins.filesystem import (
    EditFileTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
)
from pulsara_agent.tools.builtins.todo import (
    TodoTool,
    TodoValidationError,
    parse_todo_replacement,
)
from pulsara_agent.tools.builtins.artifact import ArtifactReadTool
from pulsara_agent.conversation_kernel.io import (
    KernelSessionIO,
    PhysicalToolInvocationDisposition,
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
    context_fingerprint,
    freeze_json,
    thaw_json,
)
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot
from pulsara_agent.primitives.tool_observation import (
    PhysicalToolObservationSupplement,
    ToolObservationOrigin,
    TrustedToolObservationSupplement,
    normalize_observation_duration,
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
    McpEffectKind,
    McpToolExecutionPolicyFact,
    PreparedKernelToolSurface,
    PreparedToolExecutionBinding,
    ProcessLocalToolSurfaceAccess,
    ProcessLocalToolSurfaceBorrow,
    tool_observation_origin_for_binding,
    tool_execution_surface_fingerprint,
)

from .runner import (
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


DIRECT_KERNEL_TOOL_NAMES = frozenset(
    {
        "artifact_read",
        "read_file",
        "search_files",
        "edit_file",
        "write_file",
        "todo",
        "terminal",
        "terminal_monitor",
        "terminal_process",
        "get_mcp_prompt",
        "list_mcp_prompts",
        "list_mcp_resource_templates",
        "list_mcp_resources",
        "list_mcp_servers",
        "read_mcp_resource",
    }
)


_TERMINAL_PROCESS_ACTION_EFFECTS = (
    ("list", "TERMINAL_OBSERVATION"),
    ("log", "TERMINAL_OBSERVATION"),
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

    tool_name: str
    descriptor_id: str
    descriptor_contract_version: str
    descriptor_fingerprint: str
    input_schema_fingerprint: str
    binding_contract_fingerprint: str
    catalog_entry_fingerprint: str
    availability_requirement_fingerprint: str
    permission_contract_fingerprint: str
    execution_binding_kind: str
    is_read_only: bool
    is_concurrency_safe: bool
    permission_category: str
    physical_effect_contract_fingerprint: str
    executor_identity: str
    binding_fingerprint: str


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
    input_schema_fingerprint = sha256_fingerprint(
        "production-builtin-input-schema:v1", descriptor.input_schema
    )
    physical_effect_contract = (
        {"actions": _TERMINAL_PROCESS_ACTION_EFFECTS}
        if tool_name == "terminal_process"
        else {
            "default": (
                "TERMINAL_EFFECT"
                if tool_name == "terminal"
                else entry.recovery_contract.severity
            )
        }
    )
    physical_effect_contract_fingerprint = sha256_fingerprint(
        "production-builtin-physical-effect-contract:v1",
        physical_effect_contract,
    )
    payload = {
        "tool_name": tool_name,
        "descriptor_id": descriptor.id,
        "descriptor_contract_version": contract.contract_version,
        "descriptor_fingerprint": descriptor.fingerprint(),
        "input_schema_fingerprint": input_schema_fingerprint,
        "binding_contract_fingerprint": contract.contract_fact_fingerprint,
        "catalog_entry_fingerprint": entry.entry_fingerprint,
        "availability_requirement_fingerprint": (
            entry.availability_requirement.requirement_fingerprint
        ),
        "permission_contract_fingerprint": (
            entry.permission_contract.contract_fingerprint
        ),
        "execution_binding_kind": entry.execution_binding_kind.value,
        "is_read_only": descriptor.is_read_only,
        "is_concurrency_safe": descriptor.is_concurrency_safe,
        "permission_category": descriptor.permission_category,
        "physical_effect_contract_fingerprint": (physical_effect_contract_fingerprint),
        "executor_identity": executor_identity,
    }
    return ProductionBuiltinExecutorBinding(
        tool_name=tool_name,
        descriptor_id=descriptor.id,
        descriptor_contract_version=contract.contract_version,
        descriptor_fingerprint=descriptor.fingerprint(),
        input_schema_fingerprint=input_schema_fingerprint,
        binding_contract_fingerprint=contract.contract_fact_fingerprint,
        catalog_entry_fingerprint=entry.entry_fingerprint,
        availability_requirement_fingerprint=(
            entry.availability_requirement.requirement_fingerprint
        ),
        permission_contract_fingerprint=(
            entry.permission_contract.contract_fingerprint
        ),
        execution_binding_kind=entry.execution_binding_kind.value,
        is_read_only=descriptor.is_read_only,
        is_concurrency_safe=descriptor.is_concurrency_safe,
        permission_category=descriptor.permission_category,
        physical_effect_contract_fingerprint=(physical_effect_contract_fingerprint),
        executor_identity=executor_identity,
        binding_fingerprint=sha256_fingerprint(
            "production-builtin-executor-binding:v1", payload
        ),
    )


def _qualified_executor_identity(owner: object, tool_name: str) -> str:
    owner_type = type(owner)
    return f"{owner_type.__module__}.{owner_type.__qualname__}#{tool_name}"


class KernelSubagentToolPort(Protocol):
    @property
    def tool_names(self) -> frozenset[str]: ...

    async def invoke(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        parent_turn_id: str,
    ) -> KernelToolResult: ...


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
    decision: str
    reference: str
    public_message: str
    attempt_id: str | None
    result_entry_id: str | None


class KernelToolInteractionPort(Protocol):
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


@dataclass(slots=True)
class _DirectTerminalTool:
    manager: TerminalSessionManager
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
    ) -> ToolExecutionResult:
        request = parse_terminal_input(call.arguments)
        session_id = request.terminal_session_id
        terminal = self.manager.get_or_create(
            session_id,
            owner_host_session_id=self.owner_host_session_id,
        )
        result = terminal.execute(
            TerminalRequest(
                command=request.command,
                workdir=request.workdir,
                yield_time_ms=request.yield_time_ms,
                max_output_chars=request.max_output_chars,
                tty=request.tty,
            ),
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
        )
        return _terminal_execution_result(call, result)


@dataclass(slots=True)
class _DirectTerminalProcessTool:
    manager: TerminalSessionManager
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
        if action == "log":
            log = self.manager.log_process(
                request.process_id,
                max_output_chars=maximum,
                owner_host_session_id=self.owner_host_session_id,
                since_cursor=request.since_cursor,
            )
            return _success(
                call,
                {"status": "success", **log.to_payload()},
                output_artifact_candidate=log.output_artifact_candidate,
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
                max_output_chars=maximum,
                owner_host_session_id=self.owner_host_session_id,
            )
        elif action == "submit":
            result = self.manager.write_process(
                request.process_id,
                request.data,
                append_newline=True,
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
class _DirectMcpCatalogTool:
    name: str

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        del call
        raise RuntimeError("MCP catalog tool escaped its generation-bound adapter")


@dataclass(frozen=True, slots=True)
class _PreparedMonitorSettlement:
    prepared: PreparedTerminalMonitorRegistration
    origin_attempt_id: str
    origin_result_entry_id: str


@dataclass(frozen=True, slots=True)
class _PreparedTodoSettlement:
    prepared: PreparedTodoReplacement


@dataclass(frozen=True, slots=True)
class _PendingMcpConfirmationAdmission:
    generation: int
    executor: McpBoundToolExecutor
    scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    turn_id: str
    tool_call_id: str


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
        terminal_monitor_wake_scheduler: Callable[[], None] | None = None,
        deadline_factory: KernelExecutionDeadlineFactory | None = None,
    ) -> None:
        root = workspace_root.expanduser().resolve()
        self._host_owner_id = host_owner_id
        self._session_id = session_id
        self._live_bus = live_bus
        self._physical_io = KernelSessionIO()
        self._deadlines = deadline_factory or KernelExecutionDeadlineFactory()
        self._terminal = TerminalSessionManager(
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
            ReadFileTool(root),
            SearchFilesTool(root),
            EditFileTool(root),
            WriteFileTool(root),
            TodoTool(),
            _DirectTerminalTool(self._terminal, host_owner_id),
            _DirectTerminalProcessTool(self._terminal, host_owner_id),
            _DirectTerminalMonitorTool(self._terminal_monitor),
            _DirectPlanControlTool("enter_plan"),
            _DirectPlanControlTool("ask_plan_question"),
            _DirectPlanControlTool("exit_plan"),
            _DirectMcpCatalogTool("list_mcp_servers"),
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
        self._prepared_surfaces: dict[
            tuple[int, ModelInputScopeKind, str | None], PreparedKernelToolSurface
        ] = {}
        self._closed = False
        self._physically_closed = False
        self._terminal_physically_closed = False
        self._close_async_lock = asyncio.Lock()
        self._terminal_release_task: asyncio.Task[object] | None = None
        self._terminal_monitor_close_task: asyncio.Task[object] | None = None
        self._process_local_settlements: dict[str, _PreparedMonitorSettlement] = {}
        self._todo_settlements: dict[str, _PreparedTodoSettlement] = {}
        self._todo_owner = TodoRunStateOwner(
            session_id=session_id,
            owner_epoch=host_owner_id,
        )
        self._subagent: KernelSubagentToolPort | None = None
        self._memory: KernelMemoryToolPort | None = None
        self._interaction: KernelToolInteractionPort | None = None
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

    def bind_subagent_port(self, port: KernelSubagentToolPort) -> None:
        with self._surface_lock:
            if self._subagent is not None:
                raise RuntimeError("subagent tool port is already bound")
            self._subagent = port
            self._surface_generation += 1

    def bind_memory_port(self, port: KernelMemoryToolPort) -> None:
        with self._surface_lock:
            if self._memory is not None:
                raise RuntimeError("memory tool port is already bound")
            self._memory = port
            self._surface_generation += 1

    def bind_interaction_port(self, port: KernelToolInteractionPort) -> None:
        if self._interaction is not None:
            raise RuntimeError("interaction tool port is already bound")
        self._interaction = port

    def bind_mcp_supervisor(self, supervisor: McpHostSupervisor) -> None:
        with self._surface_lock:
            if self._mcp_supervisor is not None:
                raise RuntimeError("MCP supervisor is already bound")
            if self._closed:
                raise RuntimeError("tool surface is closed")
            self._mcp_supervisor = supervisor

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

    async def reload_mcp_configs(
        self, configs: tuple[McpServerConfig, ...]
    ) -> frozenset[str]:
        """Fence a config epoch and cancel only not-yet-FULL confirmations."""

        supervisor = self._mcp_supervisor
        if supervisor is None:
            raise RuntimeError("MCP supervisor is not bound")
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
        for key, permit in tuple(self._mcp_dispatch_permits.items()):
            if (
                permit.lease._slot.server_id in disabled_or_removed  # noqa: SLF001
                and permit.state.value == "ADMITTED"
            ):
                self._mcp_dispatch_permits.pop(key, None)
                permit.release()
        return changed

    @property
    def terminal_monitor_coordinator(self) -> TerminalMonitorCoordinator:
        return self._terminal_monitor

    @property
    def todo_owner(self) -> TodoRunStateOwner:
        return self._todo_owner

    def _executor_bindings_locked(self) -> tuple[ProductionBuiltinExecutorBinding, ...]:
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

    @property
    def tool_specs(self) -> tuple[FrozenToolSpec, ...]:
        return self.snapshot_tool_surface(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        ).model_surface.tool_specs

    def snapshot_terminal_cwd(self) -> Path:
        return self._terminal.snapshot_default_cwd(
            owner_host_session_id=self._host_owner_id
        )

    def snapshot_tool_surface(
        self,
        *,
        conversation_scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
    ) -> PreparedKernelToolSurface:
        if (conversation_scope_kind is ModelInputScopeKind.ROOT) != (
            scope_subagent_task_id is None
        ):
            raise ValueError("tool surface scope identity is invalid")
        with self._surface_lock:
            if self._closed:
                raise RuntimeError("tool surface is closed")
            bindings = self._executor_bindings_locked()
            if conversation_scope_kind is ModelInputScopeKind.SUBAGENT_TASK:
                bindings = tuple(
                    binding
                    for binding in bindings
                    if binding.tool_name
                    not in {
                        "terminal_monitor",
                        "enter_plan",
                        "ask_plan_question",
                        "exit_plan",
                    }
                )
            specs: list[FrozenToolSpec] = []
            execution_bindings: list[PreparedToolExecutionBinding] = []
            for binding in bindings:
                entry = builtin_tool_catalog_entry(binding.tool_name)
                schema = freeze_json(_json_schema_value(entry.descriptor.input_schema))
                if not isinstance(schema, FrozenJsonObjectFact):
                    raise TypeError("tool schema did not freeze to an object")
                specs.append(
                    FrozenToolSpec(
                        name=binding.tool_name,
                        description=entry.descriptor.description,
                        parameters=schema,
                        descriptor_fingerprint=binding.descriptor_fingerprint,
                    )
                )
                policy = BuiltinExecutionPolicyRef(
                    tool_name=binding.tool_name,
                    catalog_entry_fingerprint=binding.catalog_entry_fingerprint,
                    policy_fingerprint=context_fingerprint(
                        "builtin-execution-policy-ref:v1",
                        {
                            "tool_name": binding.tool_name,
                            "catalog_entry_fingerprint": (
                                binding.catalog_entry_fingerprint
                            ),
                        },
                    ),
                )
                execution_bindings.append(
                    PreparedToolExecutionBinding(
                        tool_name=binding.tool_name,
                        descriptor_fingerprint=binding.descriptor_fingerprint,
                        executor_binding_fingerprint=binding.binding_fingerprint,
                        execution_policy=policy,
                        memory_citation_visibility="WORKSPACE_BOUND",
                        memory_citation_evidence_kind=(
                            "MEMORY_READ_EXPOSURE"
                            if binding.tool_name
                            in {"memory_search", "memory_get", "memory_explain"}
                            else "PRIMARY_OBSERVATION"
                        ),
                    )
                )
            mcp_runtime = self._mcp_current
            if mcp_runtime is not None:
                mcp_semantics = (
                    mcp_runtime.root_tool_specs
                    if conversation_scope_kind is ModelInputScopeKind.ROOT
                    else mcp_runtime.subagent_tool_specs
                )
                mcp_binding_by_name = {
                    item.tool_name: item for item in mcp_runtime.execution_bindings
                }
                for semantic in mcp_semantics:
                    if semantic.provider_tool_name in {item.name for item in specs}:
                        raise RuntimeError("MCP provider tool collides with a builtin")
                    specs.append(semantic.provider_spec())
                    execution_bindings.append(
                        mcp_binding_by_name[semantic.provider_tool_name]
                    )
                paired = sorted(
                    zip(specs, execution_bindings, strict=True),
                    key=lambda item: item[0].name,
                )
                specs = [item[0] for item in paired]
                execution_bindings = [item[1] for item in paired]
            frozen_specs = tuple(specs)
            if len(frozen_specs) > STRUCTURED_MODEL_INPUT_LIMITS.maximum_tool_specs:
                raise RuntimeError("MCP_DIRECT_TOOL_SURFACE_BOUND_EXCEEDED")
            fingerprint = model_tool_surface_fingerprint(
                conversation_scope_kind, frozen_specs
            )
            surface = FrozenModelToolSurface(
                conversation_scope_kind=conversation_scope_kind,
                tool_specs=frozen_specs,
                surface_fingerprint=fingerprint,
            )
            frozen_execution_bindings = tuple(execution_bindings)
            execution_fingerprint = tool_execution_surface_fingerprint(
                owner_epoch=self._surface_owner_epoch,
                surface_generation=self._surface_generation,
                semantic_surface_fingerprint=fingerprint,
                bindings=frozen_execution_bindings,
            )
            access = ProcessLocalToolSurfaceAccess(
                owner_epoch=self._surface_owner_epoch,
                surface_generation=self._surface_generation,
                conversation_scope_kind=conversation_scope_kind,
                scope_subagent_task_id=scope_subagent_task_id,
                semantic_surface_fingerprint=fingerprint,
                execution_surface_fingerprint=execution_fingerprint,
                _authority=self._surface_authority,
            )
            prepared = PreparedKernelToolSurface(
                model_surface=surface,
                execution_bindings=frozen_execution_bindings,
                execution_surface_fingerprint=execution_fingerprint,
                access=access,
            )
            self._prepared_surfaces[
                (
                    self._surface_generation,
                    conversation_scope_kind,
                    scope_subagent_task_id,
                )
            ] = prepared
            return prepared

    def borrow_tool_surface(
        self, prepared: PreparedKernelToolSurface
    ) -> ProcessLocalToolSurfaceBorrow:
        with self._surface_lock:
            self._require_prepared_surface_locked(prepared)
            if prepared.access.surface_generation != self._surface_generation:
                raise RuntimeError("retiring tool surface refuses new borrows")
            borrow_id = f"tool-surface-borrow:{uuid4().hex}"
            self._surface_borrows[borrow_id] = prepared.access.surface_generation
            return ProcessLocalToolSurfaceBorrow(
                prepared=prepared,
                borrow_id=borrow_id,
                _authority=self._surface_authority,
                _validate=self._validate_surface_borrow,
                _release=self._release_surface_borrow,
            )

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
                or not borrow.exactly_joins(prepared)
            ):
                raise RuntimeError("tool surface borrow is not active")
            self._require_prepared_surface_locked(prepared)

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
            or not retained.exactly_joins(prepared)
        ):
            raise RuntimeError("prepared tool surface is revoked")

    def _validate_surface_borrow(
        self, borrow: ProcessLocalToolSurfaceBorrow, tool_name: str
    ) -> PreparedToolExecutionBinding:
        with self._surface_lock:
            if (
                borrow._closed
                or borrow._authority is not self._surface_authority
                or borrow.borrow_id not in self._surface_borrows
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
            generation = self._surface_borrows.pop(borrow.borrow_id, None)
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
                tool_name == "remember"
                and not memory_context.memory_use_policy.allows_writes
            )
            or (
                tool_name != "remember"
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
        decision = await self._authorization_policy.decide(
            ToolDispatchAuthorizationRequest(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                arguments=dict(arguments),
                turn_id=turn_id,
                assistant_entry_id=assistant_entry_id,
                permission_snapshot=permission_snapshot,
            )
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
                f"mcp-policy:{policy.policy_fingerprint}",
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
                f"mcp-policy:{policy.policy_fingerprint}",
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
            f"mcp-policy:{policy.policy_fingerprint}",
        )

    async def request_confirmation(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
        turn_id: str,
        assistant_entry_id: str,
        permission_snapshot: FrozenRunPermissionSnapshot,
    ) -> KernelToolAuthorization:
        admission_entry = next(
            (
                (key, admission)
                for key, admission in self._mcp_confirmation_admissions.items()
                if key[1] == tool_call_id
            ),
            None,
        )
        permit_entry = next(
            (
                (key, permit)
                for key, permit in self._mcp_dispatch_permits.items()
                if key[1] == tool_call_id
            ),
            None,
        )
        if self._interaction is None:
            if admission_entry is not None:
                self._mcp_confirmation_admissions.pop(admission_entry[0], None)
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
        permit_entry = next(
            (
                (key, permit)
                for key, permit in self._mcp_dispatch_permits.items()
                if key[1] == tool_call_id
            ),
            None,
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
                if permit_entry[1].state.value == "ADMITTED":
                    permit_entry[1].release()
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.PERMISSION_DENIED,
                resolution.reference,
                resolution.public_message,
                accepted_result_entry_id=resolution.result_entry_id,
            )
        raise RuntimeError("interaction resolution vocabulary is invalid")

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
            invocation_context.tool_surface_fingerprint
            != invocation_context.surface_borrow.prepared.model_surface.surface_fingerprint
            or invocation_context.conversation_scope_kind
            != invocation_context.surface_borrow.prepared.access.conversation_scope_kind.value
            or invocation_context.scope_subagent_task_id
            != invocation_context.surface_borrow.prepared.access.scope_subagent_task_id
            or self._validate_surface_borrow(
                invocation_context.surface_borrow, tool_name
            ).executor_binding_fingerprint
            != invocation_context.executor_binding_fingerprint
        ):
            raise RuntimeError("tool invocation surface binding does not exact-join")
        binding = invocation_context.surface_borrow.execution_binding(tool_name)
        invocation_started = monotonic()
        observation_origin = tool_observation_origin_for_binding(binding)
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
            "list_mcp_prompts",
            "list_mcp_resource_templates",
            "list_mcp_resources",
            "list_mcp_servers",
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
                parent_turn_id=turn_id,
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
                    state="TOOL_UNAVAILABLE",
                    content=b'{"error":"todo scope is no longer active"}',
                    effect_class="read_only",
                    physical_observation=_freeze_physical_observation(
                        invocation_started, observation_origin
                    ),
                )
            self._todo_settlements[prepared.token_id] = _PreparedTodoSettlement(
                prepared
            )
            return KernelToolResult(
                state="SUCCESS",
                content=acknowledgement,
                process_local_settlement=ProcessLocalEffectSettlementToken(
                    prepared.token_id, prepared.token_fingerprint
                ),
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
                deadline_monotonic=self._deadlines.deadline(owner),
            )
        else:
            physical = await self._physical_io.run_tool_invocation(
                _execute_tool_call,
                tool,
                call,
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
        encoded = result.output.encode("utf-8")
        state = {
            ToolResultState.SUCCESS: "SUCCESS",
            ToolResultState.ERROR: "APPLICATION_ERROR",
            ToolResultState.INTERRUPTED: "CANCELLED",
            ToolResultState.DENIED: "APPLICATION_ERROR",
            ToolResultState.RUNNING: "SUCCESS",
        }[result.status]
        remote_identity: str | None = None
        if tool_name in {"terminal", "terminal_process"}:
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
        settlement = _PreparedMonitorSettlement(
            prepared=prepared,
            origin_attempt_id=context.attempt_id,
            origin_result_entry_id=context.result_entry_id,
        )
        self._process_local_settlements[prepared.token_id] = settlement
        token = ProcessLocalEffectSettlementToken(
            prepared.token_id, prepared.token_fingerprint
        )
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
        todo = self._todo_settlements.get(token.token_id)
        if todo is not None:
            if todo.prepared.token_fingerprint != token.token_fingerprint:
                raise RuntimeError("process-local TODO settlement token conflicts")
            if disposition is ProcessLocalEffectSettlementDisposition.COMMITTED:
                installation = self._todo_owner.commit(todo.prepared)
                outcome = ProcessLocalEffectSettlementOutcome.INSTALLED
            else:
                self._todo_owner.discard(todo.prepared)
                installation = None
                outcome = ProcessLocalEffectSettlementOutcome.DISCARDED
            self._todo_settlements.pop(token.token_id, None)
            if installation is not None:
                self._offer_todo_installation(installation)
            return ProcessLocalEffectSettlementResult(outcome)
        settlement = self._process_local_settlements.get(token.token_id)
        if settlement is None:
            if disposition is ProcessLocalEffectSettlementDisposition.COMMITTED:
                raise RuntimeError("committed process-local settlement token is absent")
            return ProcessLocalEffectSettlementResult(
                ProcessLocalEffectSettlementOutcome.DISCARDED
            )
        if settlement.prepared.token_fingerprint != token.token_fingerprint:
            raise RuntimeError("process-local settlement token conflicts")
        self._process_local_settlements.pop(token.token_id, None)
        self._terminal_monitor.settle_registration(
            token.token_id,
            token.token_fingerprint,
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
                self._surface_condition.notify_all()
            # A surface borrow can be held by an in-flight Terminal call whose
            # bounded physical wait only exits after its process group is
            # terminated.  Seal the surface first, stop those process-local
            # owners, then drain the immutable borrow before closing the
            # general thread owner.  Non-Terminal tools remain bounded by the
            # same borrow deadline and are never replaced or detached.
            close_error: BaseException | None = None
            try:
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
                runtimes = tuple(
                    dict.fromkeys(self._mcp_runtime_by_surface_generation.values())
                )
                self._mcp_runtime_by_surface_generation.clear()
                self._mcp_current = None
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
        for token_id, settlement in tuple(self._process_local_settlements.items()):
            self._terminal_monitor.settle_registration(
                token_id,
                settlement.prepared.token_fingerprint,
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
    *,
    deadline_monotonic: float,
) -> ToolExecutionResult:
    # The closed Tool API predates absolute deadlines.  KernelSessionIO owns
    # the physical thread and makes a close timeout explicit; adapters with
    # their own timeouts continue to enforce them inside execute().
    del deadline_monotonic
    return tool.execute(call)


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
        )
    return tool.execute(call, live_sink=live_sink)


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
        "error": result.error,
        "process_id": result.process_id,
        "yielded_to_background": result.status.value == "running",
        "output_disposition": result.output_disposition.value,
        "output_cursor": result.output_cursor,
        "retained_from_cursor": result.retained_from_cursor,
        "gap_before_output": result.gap_before_output,
        "truncated_by_response_bound": result.truncated_by_response_bound,
        "source_coverage": result.source_coverage.value,
        "shell_diagnostic": result.shell_diagnostic,
    }
    state = (
        ToolResultState.SUCCESS
        if result.status.value in {"success", "running"}
        else ToolResultState.ERROR
    )
    trusted_duration = (
        normalize_observation_duration(result.trusted_process_duration_microseconds)
        if action in {"start", "log", "poll", "wait"}
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
        if action in {"log", "poll", "wait"}:
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


def _json_schema_value(value: Mapping[str, object]) -> dict[str, object]:
    """Lower recursively frozen catalog values to JSON Schema containers."""

    return {str(key): _thaw_json(item) for key, item in value.items()}


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
    "DIRECT_KERNEL_TOOL_NAMES",
    "DirectKernelToolPort",
    "KernelToolInteractionPort",
    "ProductionBuiltinExecutorBinding",
]
