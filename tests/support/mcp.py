"""Deterministic test-owned MCP manager."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pulsara_agent.ports.mcp import (
    McpInvocationOwner,
    McpPendingExecutionHandleIdentity,
    McpPendingHandleState,
    McpPreparedSuspensionCommitView,
)
from pulsara_agent.event import EventContext
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.runtime_event_vocabulary import (
    PreparedMcpInputRequiredSuspension,
)
from pulsara_agent.runtime.mcp.types import (
    McpInputRequiredResolution,
    McpOriginalRequest,
    McpServerSnapshot,
)

if TYPE_CHECKING:
    from pulsara_agent.runtime.session import RuntimeSession


McpToolHandler = Callable[[dict[str, Any]], Any | Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class PreparedTestMcpPendingHandle:
    """Test-owned process-local handle for already prepared suspensions."""

    identity: McpPendingExecutionHandleIdentity
    suspension_commit_view: McpPreparedSuspensionCommitView
    state: McpPendingHandleState = McpPendingHandleState.PENDING_CONFIRMED

    def __reduce__(self):
        raise TypeError("test MCP pending handles are process-local")


def prepared_test_mcp_pending_handle(
    prepared: PreparedMcpInputRequiredSuspension,
) -> PreparedTestMcpPendingHandle:
    handle_id = f"test_mcp_pending_handle:{uuid4().hex}"
    identity_payload = {
        "handle_id": handle_id,
        "interaction_id": prepared.interaction.interaction_id,
        "binding_identity": prepared.binding_identity.model_dump(mode="json"),
        "pending_lease_reservation": (
            prepared.pending_lease_reservation.model_dump(mode="json")
        ),
        "prepared_suspension_fingerprint": (prepared.prepared_suspension_fingerprint),
        "predecessor_handle_id": None,
        "handle_generation": 1,
    }
    identity = McpPendingExecutionHandleIdentity(
        handle_id=handle_id,
        interaction_id=prepared.interaction.interaction_id,
        binding_identity=prepared.binding_identity,
        pending_lease_reservation=prepared.pending_lease_reservation,
        prepared_suspension_fingerprint=(prepared.prepared_suspension_fingerprint),
        predecessor_handle_id=None,
        handle_generation=1,
        identity_fingerprint=context_fingerprint(
            "mcp-pending-execution-handle-identity:v1", identity_payload
        ),
    )
    view_payload = {
        "interaction": prepared.interaction,
        "binding_identity": prepared.binding_identity,
        "pending_lease_reservation": prepared.pending_lease_reservation,
        "request_envelope": prepared.request_envelope,
        "deadline_monotonic": prepared.deadline_monotonic,
        "tool_observation_timing_seed": prepared.tool_observation_timing_seed,
        "prepared_suspension_fingerprint": (prepared.prepared_suspension_fingerprint),
    }
    return PreparedTestMcpPendingHandle(
        identity=identity,
        suspension_commit_view=McpPreparedSuspensionCommitView(
            **view_payload,
            view_fingerprint=context_fingerprint(
                "mcp-prepared-suspension-commit-view:v1", view_payload
            ),
        ),
    )


def install_prepared_test_mcp_pending_handle(
    *,
    runtime_session: RuntimeSession,
    supervisor: object,
    prepared: PreparedMcpInputRequiredSuspension,
    event_context: EventContext,
):
    """Install a production port around one already prepared test suspension."""

    from pulsara_agent.runtime.mcp.tool_execution_port import (
        RuntimeMcpToolExecutionPort,
    )

    port = RuntimeMcpToolExecutionPort(supervisor)  # type: ignore[arg-type]
    runtime_session.mcp_supervisor = supervisor
    runtime_session.mcp_tool_execution_port = port
    handle = port._new_handle(
        owner=McpInvocationOwner(
            runtime_session_id=runtime_session.runtime_session_id,
            run_id=event_context.run_id,
            tool_call_id=prepared.interaction.tool_call_id,
            event_context=event_context,
        ),
        prepared=prepared,
        predecessor=None,
    )
    return handle


@dataclass(slots=True)
class MockMcpClientManager:
    _snapshots: tuple[McpServerSnapshot, ...]
    handlers: dict[tuple[str, str], McpToolHandler] = field(default_factory=dict)
    close_count: int = 0
    cancel_count: int = 0
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    _closed: bool = False
    _active_tasks: set[asyncio.Task[Any]] = field(
        default_factory=set, init=False, repr=False
    )

    @property
    def snapshots(self) -> tuple[McpServerSnapshot, ...]:
        return self._snapshots

    @property
    def closed(self) -> bool:
        return self._closed

    async def call_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_ms: int,
    ) -> Any:
        if self._closed:
            raise RuntimeError("MCP manager is closed")
        self.calls.append((server_id, tool_name, dict(arguments)))
        handler = self.handlers.get((server_id, tool_name))
        if handler is None:
            raise KeyError(f"Unknown MCP tool binding: {server_id}/{tool_name}")
        task = asyncio.create_task(_await_handler(handler, dict(arguments)))
        self._active_tasks.add(task)
        try:
            return await asyncio.wait_for(task, timeout=timeout_ms / 1000)
        finally:
            self._active_tasks.discard(task)

    def cancel_active(self) -> None:
        self.cancel_count += 1
        for task in tuple(self._active_tasks):
            task.cancel()

    async def resume_suspended_request(
        self,
        *,
        server_id: str,
        original_request: McpOriginalRequest,
        request_state: str | None,
        resolution: McpInputRequiredResolution,
        timeout_ms: int,
    ) -> Any:
        del request_state
        if resolution.cancelled:
            return {"cancelled": True, "interaction_id": resolution.interaction_id}
        if original_request.tool_name is None:
            raise RuntimeError("mock MCP manager only resumes tool calls")
        return await self.call_tool(
            server_id,
            original_request.tool_name,
            original_request.arguments or {},
            timeout_ms=timeout_ms,
        )

    async def aclose(self, *, timeout_seconds: float = 5.0) -> None:
        if self._closed:
            return
        self._closed = True
        self.close_count += 1
        self.cancel_active()
        if self._active_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tuple(self._active_tasks), return_exceptions=True),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                pass


async def _await_handler(handler: McpToolHandler, arguments: dict[str, Any]) -> Any:
    result = handler(arguments)
    if hasattr(result, "__await__"):
        return await result  # type: ignore[misc]
    return result


__all__ = [
    "MockMcpClientManager",
    "PreparedTestMcpPendingHandle",
    "install_prepared_test_mcp_pending_handle",
    "prepared_test_mcp_pending_handle",
]
