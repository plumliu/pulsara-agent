"""Host-scoped Stage 2 tool surface without EventLog execution ownership."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
from threading import Lock
from typing import Mapping, Protocol

from jsonschema import ValidationError, validators

from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry
from pulsara_agent.llm.input import ToolSpec
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.terminal import parse_terminal_process_input
from pulsara_agent.ports.tool_execution import Tool, ToolCall, ToolExecutionResult
from pulsara_agent.runtime.terminal.manager import TerminalSessionManager
from pulsara_agent.runtime.terminal.models import TerminalRequest, TerminalResult
from pulsara_agent.tools.builtins.filesystem import (
    EditFileTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
)
from pulsara_agent.tools.builtins.todo import TodoTool
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
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


MAXIMUM_TOOL_RESULT_BYTES = STAGE2_LIMITS.tool_result_hard_bytes
DIRECT_KERNEL_TOOL_NAMES = frozenset(
    {
        "read_file",
        "search_files",
        "edit_file",
        "write_file",
        "todo",
        "terminal",
        "terminal_process",
    }
)


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
            return _success(call, {"status": "success", **log.to_payload()})
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
        maximum_tool_result_bytes: int = MAXIMUM_TOOL_RESULT_BYTES,
    ) -> None:
        if not 1 <= maximum_tool_result_bytes <= MAXIMUM_TOOL_RESULT_BYTES:
            raise ValueError("tool result bound is outside the Stage 2 contract")
        root = workspace_root.expanduser().resolve()
        self._host_owner_id = host_owner_id
        self._session_id = session_id
        self._live_bus = live_bus
        self._maximum_tool_result_bytes = maximum_tool_result_bytes
        self._terminal = TerminalSessionManager(
            workspace_root=root,
            max_pending_completion_records=0,
        )
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
        self._tools = {tool.name: tool for tool in tools}
        self._authorization_policy = authorization_policy
        self._close_lock = Lock()
        self._closed = False
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
        names = set(self._tools)
        if self._subagent is not None:
            names.update(self._subagent.tool_names)
        if self._memory is not None:
            names.update(self._memory.tool_names)
        return tuple(
            ToolSpec(
                name=name,
                description=builtin_tool_catalog_entry(name).descriptor.description,
                parameters=_json_schema_value(
                    builtin_tool_catalog_entry(name).descriptor.input_schema
                ),
            )
            for name in sorted(names)
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
        result = await asyncio.to_thread(tool.execute, call)
        encoded = _truncate_tool_result_utf8(
            result.output,
            maximum_bytes=self._maximum_tool_result_bytes,
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
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        await asyncio.wait_for(
            asyncio.to_thread(
                self._terminal.release_owner,
                self._host_owner_id,
                completion_drain_timeout_seconds=min(timeout_seconds, 1.0),
            ),
            timeout=timeout_seconds,
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
    )


def _truncate_tool_result_utf8(output: str, *, maximum_bytes: int) -> bytes:
    """Return a valid UTF-8 tool result whose final carrier obeys the hard cap."""

    if maximum_bytes < 1:
        raise ValueError("tool result byte cap must be positive")
    raw = output.encode("utf-8", errors="replace")
    if len(raw) <= maximum_bytes:
        return raw
    if maximum_bytes < len(b"[cut]"):
        return b"~"[:maximum_bytes]

    # The omitted count includes bytes removed to make room for the marker.
    # Recompute until the digit-width-dependent suffix and prefix agree.
    omitted = len(raw)
    for _ in range(4):
        suffix = f"\n[tool output truncated: {omitted} bytes omitted]".encode("ascii")
        if len(suffix) > maximum_bytes:
            return b"[cut]"[:maximum_bytes]
        prefix_budget = maximum_bytes - len(suffix)
        prefix = raw[:prefix_budget].decode("utf-8", errors="ignore").encode("utf-8")
        next_omitted = len(raw) - len(prefix)
        if next_omitted == omitted:
            result = prefix + suffix
            if len(result) > maximum_bytes:
                raise AssertionError("tool result truncation exceeded its hard cap")
            result.decode("utf-8")
            return result
        omitted = next_omitted
    raise AssertionError("tool result truncation did not converge")


def _success(call: ToolCall, payload: Mapping[str, object]) -> ToolExecutionResult:
    return ToolExecutionResult(
        call_id=call.id,
        tool_name=call.name,
        status=ToolResultState.SUCCESS,
        output=json.dumps(dict(payload), ensure_ascii=False),
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
    "MAXIMUM_TOOL_RESULT_BYTES",
]
