"""Host-scoped tool surface with process-local physical execution ownership."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
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
    model_tool_surface_fingerprint,
)
from pulsara_agent.message import ToolResultState
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
from pulsara_agent.tools.builtins.todo import TodoTool
from pulsara_agent.tools.builtins.artifact import ArtifactReadTool
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.live import (
    LiveAgentEventBus,
    LiveBlockKind,
    LiveChannelKind,
)
from pulsara_agent.ports.live_agent_event import (
    TerminalProcessCompletedPayload,
    live_digest,
)
from pulsara_agent.primitives.model_call import sha256_fingerprint
from pulsara_agent.primitives.context import FrozenJsonObjectFact, freeze_json
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot
from pulsara_agent.conversation_kernel.vocabulary import LiveEventType
from pulsara_agent.conversation_kernel.tool_policy import (
    ToolDispatchAuthorizationPolicy,
    ToolDispatchAuthorizationRequest,
    ToolDispatchDecisionKind,
)
from pulsara_agent.conversation_kernel.tool_surface import (
    PreparedKernelToolSurface,
    ProcessLocalToolSurfaceAccess,
    ProcessLocalToolSurfaceBorrow,
)

from .runner import (
    KernelToolInvocationContext,
    KernelToolLiveSink,
    KernelToolAuthorization,
    KernelToolAuthorizationKind,
    KernelToolResult,
    ProcessLocalEffectSettlementDisposition,
    ProcessLocalEffectSettlementToken,
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
    }
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
        assistant_entry_id: str,
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
    ) -> KernelToolInteractionResolution: ...


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


@dataclass(frozen=True, slots=True)
class _PreparedMonitorSettlement:
    prepared: PreparedTerminalMonitorRegistration
    origin_attempt_id: str
    origin_result_entry_id: str


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
    ) -> None:
        root = workspace_root.expanduser().resolve()
        self._host_owner_id = host_owner_id
        self._session_id = session_id
        self._live_bus = live_bus
        self._physical_io = KernelSessionIO()
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
        self._surface_borrows: set[str] = set()
        self._closed = False
        self._physically_closed = False
        self._terminal_physically_closed = False
        self._close_async_lock = asyncio.Lock()
        self._terminal_release_task: asyncio.Task[object] | None = None
        self._terminal_monitor_close_task: asyncio.Task[object] | None = None
        self._process_local_settlements: dict[str, _PreparedMonitorSettlement] = {}
        self._subagent: KernelSubagentToolPort | None = None
        self._memory: KernelMemoryToolPort | None = None
        self._interaction: KernelToolInteractionPort | None = None

    def bind_subagent_port(self, port: KernelSubagentToolPort) -> None:
        with self._surface_lock:
            if self._subagent is not None:
                raise RuntimeError("subagent tool port is already bound")
            if self._surface_borrows:
                raise RuntimeError("tool surface cannot change during an active borrow")
            self._subagent = port
            self._surface_generation += 1

    def bind_memory_port(self, port: KernelMemoryToolPort) -> None:
        with self._surface_lock:
            if self._memory is not None:
                raise RuntimeError("memory tool port is already bound")
            if self._surface_borrows:
                raise RuntimeError("tool surface cannot change during an active borrow")
            self._memory = port
            self._surface_generation += 1

    def bind_interaction_port(self, port: KernelToolInteractionPort) -> None:
        if self._interaction is not None:
            raise RuntimeError("interaction tool port is already bound")
        self._interaction = port

    @property
    def terminal_monitor_coordinator(self) -> TerminalMonitorCoordinator:
        return self._terminal_monitor

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
            for binding in bindings:
                schema = freeze_json(
                    _json_schema_value(
                        builtin_tool_catalog_entry(
                            binding.tool_name
                        ).descriptor.input_schema
                    )
                )
                if not isinstance(schema, FrozenJsonObjectFact):
                    raise TypeError("tool schema did not freeze to an object")
                specs.append(
                    FrozenToolSpec(
                        name=binding.tool_name,
                        description=builtin_tool_catalog_entry(
                            binding.tool_name
                        ).descriptor.description,
                        parameters=schema,
                        descriptor_fingerprint=binding.descriptor_fingerprint,
                        executor_binding_fingerprint=binding.binding_fingerprint,
                    )
                )
            frozen_specs = tuple(specs)
            fingerprint = model_tool_surface_fingerprint(
                conversation_scope_kind, frozen_specs
            )
            surface = FrozenModelToolSurface(
                conversation_scope_kind=conversation_scope_kind,
                tool_specs=frozen_specs,
                surface_fingerprint=fingerprint,
            )
            access = ProcessLocalToolSurfaceAccess(
                owner_epoch=self._surface_owner_epoch,
                surface_generation=self._surface_generation,
                conversation_scope_kind=conversation_scope_kind,
                scope_subagent_task_id=scope_subagent_task_id,
                surface_fingerprint=fingerprint,
                _authority=self._surface_authority,
            )
            return PreparedKernelToolSurface(
                model_surface=surface,
                executor_binding_fingerprints=tuple(
                    binding.binding_fingerprint for binding in bindings
                ),
                access=access,
            )

    def borrow_tool_surface(
        self, prepared: PreparedKernelToolSurface
    ) -> ProcessLocalToolSurfaceBorrow:
        with self._surface_lock:
            self._require_prepared_surface_locked(prepared)
            borrow_id = f"tool-surface-borrow:{uuid4().hex}"
            self._surface_borrows.add(borrow_id)
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
        if (
            self._closed
            or access._authority is not self._surface_authority
            or access.owner_epoch != self._surface_owner_epoch
            or access.surface_generation != self._surface_generation
        ):
            raise RuntimeError("prepared tool surface is revoked")
        current = self.snapshot_tool_surface(
            conversation_scope_kind=access.conversation_scope_kind,
            scope_subagent_task_id=access.scope_subagent_task_id,
        )
        if (
            current.model_surface != prepared.model_surface
            or current.executor_binding_fingerprints
            != prepared.executor_binding_fingerprints
        ):
            raise RuntimeError("prepared tool surface binding drifted")

    def _validate_surface_borrow(
        self, borrow: ProcessLocalToolSurfaceBorrow, tool_name: str
    ) -> str:
        with self._surface_lock:
            if (
                borrow._closed
                or borrow._authority is not self._surface_authority
                or borrow.borrow_id not in self._surface_borrows
            ):
                raise RuntimeError("tool surface borrow is not active")
            self._require_prepared_surface_locked(borrow.prepared)
            for tool in borrow.prepared.model_surface.tool_specs:
                if tool.name == tool_name:
                    return tool.executor_binding_fingerprint
        raise RuntimeError("tool was not advertised by the prepared surface")

    def _release_surface_borrow(self, borrow: ProcessLocalToolSurfaceBorrow) -> None:
        with self._surface_condition:
            if borrow._authority is not self._surface_authority:
                raise RuntimeError("tool surface borrow authority conflicts")
            self._surface_borrows.discard(borrow.borrow_id)
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
    ) -> KernelToolAuthorization:
        try:
            self._validate_surface_borrow(surface_borrow, tool_name)
        except RuntimeError:
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                "tool-surface:revoked",
                f"tool unavailable: {tool_name}",
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
        return KernelToolAuthorization(
            KernelToolAuthorizationKind.ALLOW,
            f"descriptor:{entry.descriptor.id}:{entry.entry_fingerprint}",
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
        if self._interaction is None:
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.PERMISSION_DENIED,
                "interaction:no-controller-owner",
                "tool execution requires confirmation but no controller is attached",
            )
        resolution = await self._interaction.request_tool_confirmation(
            turn_id=turn_id,
            assistant_entry_id=assistant_entry_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            permission_snapshot=permission_snapshot,
        )
        if resolution.decision == "ALLOW":
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
            )
            != invocation_context.executor_binding_fingerprint
        ):
            raise RuntimeError("tool invocation surface binding does not exact-join")
        if self._subagent is not None and tool_name in self._subagent.tool_names:
            return await self._subagent.invoke(
                tool_name=tool_name,
                arguments=arguments,
                parent_turn_id=turn_id,
            )
        if self._memory is not None and tool_name in self._memory.tool_names:
            return await self._memory.invoke(
                tool_name=tool_name,
                arguments=arguments,
                assistant_entry_id=assistant_entry_id,
            )
        tool = self._tools[tool_name]
        if isinstance(tool, _DirectPlanControlTool):
            raise RuntimeError("Plan control escaped the runner batch barrier")
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
            result = await self._physical_io.run(
                _execute_terminal_tool_call,
                tool,
                call,
                live_sink,
                origin,
                deadline_monotonic=(
                    monotonic() + STAGE2_LIMITS.foreground_io_timeout_ms / 1_000
                ),
            )
        else:
            result = await self._physical_io.run(
                _execute_tool_call,
                tool,
                call,
                deadline_monotonic=(
                    monotonic() + STAGE2_LIMITS.foreground_io_timeout_ms / 1_000
                ),
            )
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
    ) -> None:
        settlement = self._process_local_settlements.get(token.token_id)
        if settlement is None:
            return
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
            await self._stop_terminal_physical_owners_locked(deadline)
            await asyncio.to_thread(self._wait_for_surface_borrows, deadline)
            await self._physical_io.aclose(deadline_monotonic=deadline)
            self._physically_closed = True

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
                asyncio.to_thread(
                    self._terminal_monitor.stop_admission_and_close,
                    timeout_seconds=max(0.001, deadline_monotonic - monotonic()),
                )
            )
        await asyncio.wait_for(
            asyncio.shield(self._terminal_monitor_close_task),
            timeout=max(0.001, deadline_monotonic - monotonic()),
        )
        self._terminal_monitor_close_task.result()
        if self._terminal_release_task is None:
            self._terminal_release_task = asyncio.create_task(
                asyncio.to_thread(
                    self._terminal.release_owner,
                    self._host_owner_id,
                    timeout_seconds=max(0.001, deadline_monotonic - monotonic()),
                )
            )
        await asyncio.wait_for(
            asyncio.shield(self._terminal_release_task),
            timeout=max(0.001, deadline_monotonic - monotonic()),
        )
        self._terminal_release_task.result()
        self._terminal_physically_closed = True

    def _wait_for_surface_borrows(self, deadline_monotonic: float) -> None:
        with self._surface_condition:
            while self._surface_borrows:
                remaining = deadline_monotonic - monotonic()
                if remaining <= 0:
                    raise TimeoutError("tool surface borrows did not drain")
                self._surface_condition.wait(timeout=remaining)


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


def _execute_terminal_tool_call(
    tool: _DirectTerminalTool | _DirectTerminalProcessTool,
    call: ToolCall,
    live_sink: KernelToolLiveSink | None,
    origin: TerminalProcessOrigin,
    *,
    deadline_monotonic: float,
) -> ToolExecutionResult:
    del deadline_monotonic
    if isinstance(tool, _DirectTerminalTool):
        return tool.execute(call, live_sink=live_sink, origin=origin)
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
    return ToolExecutionResult(
        call_id=call.id,
        tool_name=call.name,
        status=state,
        output=json.dumps(payload, ensure_ascii=False),
        output_artifact_candidate=result.output_artifact_candidate,
    )


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
