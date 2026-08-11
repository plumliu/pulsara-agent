"""Host-scoped tool surface with process-local physical execution ownership."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Mapping, Protocol

from jsonschema import ValidationError, validators

from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry
from pulsara_agent.llm.input import ToolSpec
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.artifact import ToolArtifactReadPort
from pulsara_agent.ports.terminal import parse_terminal_process_input
from pulsara_agent.ports.tool_execution import (
    Tool,
    ToolCall,
    ToolExecutionResult,
    ToolOutputArtifactCandidate,
)
from pulsara_agent.terminal_process import (
    TerminalRequest,
    TerminalResult,
    TerminalSessionManager,
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
    TerminalMonitorClosedPayload,
    TerminalMonitorObservationPayload,
    TerminalMonitorOpenedPayload,
    TerminalProcessCompletedPayload,
    live_digest,
)
from pulsara_agent.primitives.model_call import sha256_fingerprint
from pulsara_agent.conversation_kernel.vocabulary import LiveEventType
from pulsara_agent.conversation_kernel.tool_policy import (
    ToolDispatchAuthorizationPolicy,
    ToolDispatchAuthorizationRequest,
    ToolDispatchDecisionKind,
)

from .runner import (
    KernelToolAuthorization,
    KernelToolAuthorizationKind,
    KernelToolResult,
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

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        arguments = call.arguments
        session_id = str(arguments.get("terminal_session_id") or "default")
        terminal = self.manager.get_or_create(
            session_id,
            owner_host_session_id=self.owner_host_session_id,
        )
        result = terminal.execute(
            TerminalRequest(
                command=str(arguments["command"]),
                workdir=_optional_string(arguments, "workdir"),
                yield_time_ms=int(arguments.get("yield_time_ms", 10_000)),
                max_output_chars=int(arguments.get("max_output_chars", 32_000)),
                tty=bool(arguments.get("tty", False)),
            )
        )
        return _terminal_execution_result(call, result)


@dataclass(slots=True)
class _DirectTerminalProcessTool:
    manager: TerminalSessionManager
    owner_host_session_id: str
    name: str = "terminal_process"

    def execute(self, call: ToolCall) -> ToolExecutionResult:
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
            )
        elif action == "wait":
            result = self.manager.wait_process(
                request.process_id,
                timeout_seconds=request.timeout_seconds,
                max_output_chars=maximum,
                owner_host_session_id=self.owner_host_session_id,
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
    ) -> None:
        root = workspace_root.expanduser().resolve()
        self._host_owner_id = host_owner_id
        self._session_id = session_id
        self._live_bus = live_bus
        self._physical_io = KernelSessionIO()
        self._terminal = TerminalSessionManager(workspace_root=root)
        self._terminal.activate_owner(host_owner_id)
        tools: tuple[Tool, ...] = (
            ReadFileTool(root),
            SearchFilesTool(root),
            EditFileTool(root),
            WriteFileTool(root),
            TodoTool(),
            _DirectTerminalTool(self._terminal, host_owner_id),
            _DirectTerminalProcessTool(self._terminal, host_owner_id),
        )
        if artifact_read_port is not None:
            tools = (*tools, ArtifactReadTool(artifact_read_port))
        self._tools = {tool.name: tool for tool in tools}
        self._authorization_policy = authorization_policy
        self._close_lock = Lock()
        self._closed = False
        self._physically_closed = False
        self._close_async_lock = asyncio.Lock()
        self._terminal_release_task: asyncio.Task[object] | None = None
        self._subagent: KernelSubagentToolPort | None = None
        self._memory: KernelMemoryToolPort | None = None
        self._interaction: KernelToolInteractionPort | None = None

    def bind_subagent_port(self, port: KernelSubagentToolPort) -> None:
        if self._subagent is not None:
            raise RuntimeError("subagent tool port is already bound")
        self._subagent = port

    def bind_memory_port(self, port: KernelMemoryToolPort) -> None:
        if self._memory is not None:
            raise RuntimeError("memory tool port is already bound")
        self._memory = port

    def bind_interaction_port(self, port: KernelToolInteractionPort) -> None:
        if self._interaction is not None:
            raise RuntimeError("interaction tool port is already bound")
        self._interaction = port

    @property
    def tool_specs(self) -> tuple[ToolSpec, ...]:
        bindings = self.executor_bindings
        return tuple(
            ToolSpec(
                name=binding.tool_name,
                description=builtin_tool_catalog_entry(
                    binding.tool_name
                ).descriptor.description,
                parameters=_json_schema_value(
                    builtin_tool_catalog_entry(
                        binding.tool_name
                    ).descriptor.input_schema
                ),
            )
            for binding in bindings
        )

    @property
    def executor_bindings(self) -> tuple[ProductionBuiltinExecutorBinding, ...]:
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

    async def authorize(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        tool_call_id: str,
        turn_id: str,
        assistant_entry_id: str,
    ) -> KernelToolAuthorization:
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
        )
        if resolution.decision == "ALLOW":
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.ALLOW,
                resolution.reference,
                resolution.public_message,
                accepted_attempt_id=resolution.attempt_id,
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
    ) -> KernelToolResult:
        if self._closed:
            raise RuntimeError("tool surface is closed")
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
        call = ToolCall(
            id=tool_call_id,
            name=tool_name,
            arguments=dict(arguments),
        )
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
                    self._offer_terminal_live(
                        process_id=process_id,
                        attempt_id=attempt_id,
                        turn_id=turn_id,
                        payload=payload,
                    )
        return KernelToolResult(
            state=state,
            content=encoded,
            remote_identity=remote_identity,
            output_artifact_candidate=result.output_artifact_candidate,
            artifact_source_read=result.artifact_source_read,
        )

    def _offer_terminal_live(
        self,
        *,
        process_id: str,
        attempt_id: str,
        turn_id: str,
        payload: Mapping[str, object],
    ) -> None:
        monitor_id = f"terminal-monitor:{attempt_id}"
        generation_id = f"terminal:{attempt_id}"
        status = str(payload.get("status") or "unknown")
        output = str(payload.get("output") or "")
        digest = live_digest(output)
        common = {
            "session_id": self._session_id,
            "turn_id": turn_id,
            "draft_identity": generation_id,
            "scope_kind": "ROOT",
            "channel_kind": LiveChannelKind.TERMINAL_EXTENSION,
            "generation_id": generation_id,
            "block_id": monitor_id,
            "block_ordinal": 0,
            "block_kind": LiveBlockKind.OPERATIONAL,
        }
        if status == "running":
            self._live_bus.offer_nowait(
                event_type=LiveEventType.TERMINAL_MONITOR_OPENED,
                payload=TerminalMonitorOpenedPayload(monitor_id, process_id),
                **common,
            )
        self._live_bus.offer_nowait(
            event_type=LiveEventType.TERMINAL_MONITOR_OBSERVATION,
            payload=TerminalMonitorObservationPayload(
                monitor_id,
                process_id,
                "PROGRESS" if status == "running" else "COMPLETION",
                output[: STAGE2_LIMITS.tool_argument_display_hard_bytes],
                len(output.encode("utf-8")),
                digest,
            ),
            **common,
        )
        if status != "running":
            exit_code = payload.get("exit_code")
            self._live_bus.offer_nowait(
                event_type=LiveEventType.TERMINAL_PROCESS_COMPLETED,
                payload=TerminalProcessCompletedPayload(
                    process_id,
                    status,
                    exit_code if isinstance(exit_code, int) else None,
                    len(output.encode("utf-8")),
                    digest,
                ),
                **common,
            )
            self._live_bus.offer_nowait(
                event_type=LiveEventType.TERMINAL_MONITOR_CLOSED,
                payload=TerminalMonitorClosedPayload(
                    monitor_id, process_id, "PROCESS_TERMINAL"
                ),
                **common,
            )

    async def aclose(self, *, timeout_seconds: float = 5.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("tool close timeout must be positive")
        async with self._close_async_lock:
            if self._physically_closed:
                return
            with self._close_lock:
                self._closed = True
            deadline = monotonic() + timeout_seconds
            await self._physical_io.aclose(deadline_monotonic=deadline)
            if self._terminal_release_task is None:
                self._terminal_release_task = asyncio.create_task(
                    asyncio.to_thread(
                        self._terminal.release_owner,
                        self._host_owner_id,
                        timeout_seconds=max(0.001, deadline - monotonic()),
                    )
                )
            await asyncio.wait_for(
                asyncio.shield(self._terminal_release_task),
                timeout=max(0.001, deadline - monotonic()),
            )
            self._terminal_release_task.result()
            self._physically_closed = True


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


def _optional_string(arguments: Mapping[str, object], key: str) -> str | None:
    value = arguments.get(key)
    return value if isinstance(value, str) and value else None


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
