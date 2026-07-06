"""Long-lived conversation session wrapper around AgentRuntime."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, AsyncIterator, Awaitable, Callable
from uuid import uuid4

from pulsara_agent.event import (
    AgentEvent,
    ContextCompactionCompletedEvent,
    ContextCompactionFailedEvent,
    ContextCompactionStartedEvent,
    PlanExitResolvedEvent,
    PlanModeEnteredEvent,
    PlanModeExitedEvent,
)
from pulsara_agent.host.identity import ResolvedWorkspace
from pulsara_agent.host.transcript import rebuild_prior_messages
from pulsara_agent.message import SystemMsg
from pulsara_agent.runtime.approval import (
    ApprovalResolution,
    PendingApproval,
    pending_approval_from_state,
)
from pulsara_agent.runtime.agent import AgentRunResult
from pulsara_agent.runtime.permission import (
    ApprovalPolicy,
    EffectivePermissionPolicy,
    PermissionMode,
    PermissionProfile,
    TerminalAccess,
    default_permission_policy,
    parse_permission_mode,
    preset_to_policy,
)
from pulsara_agent.runtime.plan import (
    McpElicitationResolution,
    McpInputRequiredInteractionResolution,
    PLAN_ACTIVE_INSTRUCTION,
    PLAN_ACTIVE_INSTRUCTION_NAME,
    PLAN_ENTRY_INSTRUCTION,
    PLAN_ENTRY_INSTRUCTION_NAME,
    PendingInteraction,
    PendingMcpElicitation,
    PendingMcpInputRequired,
    PendingPlanInteraction,
    PlanInteractionResolution,
    PlanWorkflowState,
    pending_plan_interaction_from_state,
    pending_mcp_elicitation_from_state,
    pending_mcp_input_required_from_state,
    reduce_plan_workflow_state,
)
from pulsara_agent.runtime.recovery import AbortKind, StopRequest
from pulsara_agent.runtime.state import LoopState, LoopStatus
from pulsara_agent.runtime.mcp.store import load_mcp_server_configs
from pulsara_agent.runtime.mcp.supervisor import McpServerSupervisor
from pulsara_agent.runtime.terminal import WorkspaceTerminalLease
from pulsara_agent.runtime.wiring import AgentRuntimeWiring
from pulsara_agent.capability.providers.mcp import McpCapabilityBindingBundle, McpCapabilityProvider, build_mcp_bundle
from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.tools.adapters.mcp import McpCapabilityTool


class HostSessionBusyError(RuntimeError):
    """Raised when a HostSession already has an active run."""


class HostSessionPendingApprovalError(RuntimeError):
    """Raised when a HostSession is suspended on a pending approval."""


class HostSessionPendingInteractionError(RuntimeError):
    """Raised when a HostSession is suspended on any pending user interaction."""


class HostSessionLifecycle(StrEnum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


class _StreamError:
    __slots__ = ("exc",)

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


_STREAM_DONE = object()
_STREAM_QUEUE_MAX_ITEMS = 128


def _replace_mcp_tool_bindings(current: tuple[object, ...], mcp_tools: tuple[object, ...]) -> tuple[object, ...]:
    return (
        *(tool for tool in current if not isinstance(tool, McpCapabilityTool)),
        *mcp_tools,
    )


def _replace_mcp_capability_provider(
    current: CapabilityRuntime,
    mcp_bundle: McpCapabilityBindingBundle | None,
) -> CapabilityRuntime:
    providers = []
    inserted = False
    for provider in current.providers:
        if isinstance(provider, McpCapabilityProvider):
            if mcp_bundle is not None and not inserted:
                providers.append(McpCapabilityProvider(mcp_bundle))
                inserted = True
            continue
        providers.append(provider)
    if mcp_bundle is not None and not inserted:
        providers.append(McpCapabilityProvider(mcp_bundle))
    return CapabilityRuntime(providers=tuple(providers))


class _StreamObserver:
    """Bounded, detachable observation channel for one Host-owned run."""

    __slots__ = ("attached", "queue")

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=_STREAM_QUEUE_MAX_ITEMS)
        self.attached = True

    async def emit(self, item: Any) -> None:
        if self.attached:
            await self.queue.put(item)

    def detach(self) -> None:
        # Mark detached before making space. A producer already blocked in put()
        # may enqueue one final item after the drain, but every subsequent emit
        # becomes a no-op and the queue remains bounded.
        self.attached = False
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return


@dataclass(slots=True)
class HostSession:
    host_session_id: str
    conversation_id: str
    workspace: ResolvedWorkspace
    wiring: AgentRuntimeWiring
    terminal_lease: WorkspaceTerminalLease | None = None
    mcp_supervisor: McpServerSupervisor | None = None
    created_at: float = field(default_factory=time.monotonic)
    last_active_at: float = field(default_factory=time.monotonic)
    active_run_id: str | None = None
    stopping_run_id: str | None = None
    suspended_run_id: str | None = None
    pending_interaction: PendingInteraction | None = None
    plan_state: PlanWorkflowState = field(default_factory=PlanWorkflowState)
    _lifecycle: HostSessionLifecycle = field(default=HostSessionLifecycle.OPEN)
    _suspended_state: LoopState | None = None
    _active_state: LoopState | None = None
    _active_task: asyncio.Task[Any] | None = None
    _compaction_listeners: list[Callable[[AgentEvent], None]] = field(default_factory=list, init=False, repr=False)
    _run_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _stop_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        reduced = reduce_plan_workflow_state(self.wiring.runtime_wiring.event_log.iter())
        if reduced.active or reduced.latest_accepted_plan_summary or reduced.latest_accepted_plan_artifact_id:
            self.plan_state = reduced
        if self.plan_state.active:
            self.wiring.agent_runtime.set_permission_policy(
                preset_to_policy(PermissionMode.READ_ONLY),
                mode=PermissionMode.READ_ONLY,
            )

    @property
    def closed(self) -> bool:
        return self._lifecycle is HostSessionLifecycle.CLOSED

    @property
    def lifecycle(self) -> HostSessionLifecycle:
        return self._lifecycle

    def begin_close(self) -> None:
        """Synchronously close the mutation gate before async teardown starts."""
        if self._lifecycle is HostSessionLifecycle.OPEN:
            self._lifecycle = HostSessionLifecycle.CLOSING

    @property
    def runtime_session_id(self) -> str:
        return self.wiring.runtime_wiring.runtime_session.runtime_session_id

    @property
    def has_live_processes(self) -> bool:
        return self.wiring.runtime_wiring.runtime_session.terminal_sessions.has_live_processes(
            owner_host_session_id=self.host_session_id
        )

    @property
    def terminal_summary(self) -> dict[str, object]:
        terminal_sessions = self.wiring.runtime_wiring.runtime_session.terminal_sessions
        processes = terminal_sessions.list_processes(owner_host_session_id=self.host_session_id)
        return {
            "has_live_processes": terminal_sessions.has_live_processes(owner_host_session_id=self.host_session_id),
            "live_process_count": terminal_sessions.live_process_count(owner_host_session_id=self.host_session_id),
            "finished_process_count": terminal_sessions.finished_process_count(owner_host_session_id=self.host_session_id),
            "processes": [process.to_payload() for process in processes],
        }

    async def _sync_mcp_servers_for_turn(self) -> None:
        """Refresh session-owned MCP descriptors and bindings at safe points.

        The supervisor owns reconnect/backoff state.  Each turn/resume uses the
        latest supervisor snapshot and then atomically rebuilds both surfaces:
        the model-facing capability provider and the ToolRegistry execution
        bindings.  A failed/disabled server remains visible as diagnostics.
        """
        supervisor = self.mcp_supervisor
        if supervisor is None:
            return
        configs = load_mcp_server_configs(workspace_root=self.workspace.workspace_root)
        manager = await supervisor.sync_servers(configs)
        mcp_bundle = build_mcp_bundle(manager) if manager is not None else None
        runtime_session = self.wiring.runtime_wiring.runtime_session
        runtime_session.extra_tool_bindings = _replace_mcp_tool_bindings(
            runtime_session.extra_tool_bindings,
            mcp_bundle.tools if mcp_bundle is not None else (),
        )
        self.wiring = replace(
            self.wiring,
            runtime_wiring=replace(
                self.wiring.runtime_wiring,
                mcp_manager=manager,
                mcp_bundle=mcp_bundle,
            ),
        )
        self.wiring.agent_runtime.refresh_capability_runtime(
            _replace_mcp_capability_provider(self.wiring.agent_runtime.capability_runtime, mcp_bundle)
        )
        if self._suspended_state is not None:
            self._suspended_state.scratchpad.pop("capability_exposure", None)
        if self._active_state is not None:
            self._active_state.scratchpad.pop("capability_exposure", None)

    # -- Run entry points -----------------------------------------------------
    # run_turn / stream_turn / approval resume / plan resume all flow through one
    # internal execution handle (_run_owned / _stream_owned). The HostSession owns
    # the task and cancel scope; whether the caller consumes a result or an event
    # stream is only an observation difference and never changes stop/drain/close
    # semantics (contract §6.1).

    async def run_turn(
        self,
        user_input: str,
        *,
        active_skill_names: frozenset[str] | None = None,
    ) -> AgentRunResult:
        self._raise_if_not_open("starting a new turn")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        self._raise_if_pending_interaction("starting a new turn")
        self._raise_if_active_run()
        async with self._run_lock:
            await self._sync_mcp_servers_for_turn()
            prior_messages = await self._prepare_prior_messages_for_turn(user_input)
            prior_messages.extend(self._plan_runtime_messages())
            state = self._begin_active_state()
            return await self._run_owned(
                state,
                lambda: self.wiring.agent_runtime.run_task(
                    user_input,
                    prior_messages=prior_messages,
                    state=state,
                    active_skill_names=active_skill_names,
                ),
            )

    async def stream_turn(
        self,
        user_input: str,
        *,
        active_skill_names: frozenset[str] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        self._raise_if_not_open("starting a new turn")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        self._raise_if_pending_interaction("starting a new turn")
        self._raise_if_active_run()
        async with self._run_lock:
            await self._sync_mcp_servers_for_turn()
            prior_messages = await self._prepare_prior_messages_for_turn(user_input)
            prior_messages.extend(self._plan_runtime_messages())
            state = self._begin_active_state()
            async for event in self._stream_owned(
                state,
                lambda: self.wiring.agent_runtime.stream_task(
                    user_input,
                    prior_messages=prior_messages,
                    state=state,
                    active_skill_names=active_skill_names,
                ),
            ):
                yield event

    def get_pending_approval(self) -> PendingApproval | None:
        return self.pending_interaction if isinstance(self.pending_interaction, PendingApproval) else None

    def get_pending_interaction(self) -> PendingInteraction | None:
        return self.pending_interaction

    @property
    def current_permission_mode(self) -> PermissionMode | None:
        return self.wiring.agent_runtime.permission_mode

    def current_permission_policy(self) -> EffectivePermissionPolicy:
        return self.wiring.agent_runtime.permission_policy

    def set_permission_mode(self, mode: str | PermissionMode) -> EffectivePermissionPolicy:
        """Switch the conversation's permission mode at a turn boundary.

        Only the user/host may call this; the agent has no self-switch tool.
        Rejected while a run is active, a turn is stopping, or an approval is
        pending, so the switch never corrupts an in-flight turn. Takes effect
        on the next turn (gate) / next execution (terminal tools)."""
        self._raise_if_not_open("switching mode")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        self._raise_if_pending_interaction("switching mode")
        self._raise_if_active_run()
        if self.plan_state.active:
            raise ValueError(
                "cannot switch permission mode while plan workflow is active; "
                "approve, cancel, or force-exit the plan first"
            )
        parsed = parse_permission_mode(mode)
        policy = preset_to_policy(parsed)
        self.wiring.agent_runtime.set_permission_policy(policy, mode=parsed)
        self.last_active_at = time.monotonic()
        return policy

    async def resolve_approval(self, resolution: ApprovalResolution) -> AgentRunResult:
        self._raise_if_not_open("resolving an approval")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        pending = self._require_pending_approval(resolution.approval_id)
        self._raise_if_active_run()
        async with self._run_lock:
            await self._sync_mcp_servers_for_turn()
            state = self._resume_active_state(pending)
            return await self._run_owned(
                state,
                lambda: self.wiring.agent_runtime.resume_after_approval(state, resolution),
            )

    async def stream_approval_resolution(
        self,
        resolution: ApprovalResolution,
    ) -> AsyncIterator[AgentEvent]:
        self._raise_if_not_open("resolving an approval")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        pending = self._require_pending_approval(resolution.approval_id)
        self._raise_if_active_run()
        async with self._run_lock:
            await self._sync_mcp_servers_for_turn()
            state = self._resume_active_state(pending)
            async for event in self._stream_owned(
                state,
                lambda: self.wiring.agent_runtime.stream_after_approval(state, resolution),
            ):
                yield event

    async def resolve_plan_interaction(
        self,
        resolution: PlanInteractionResolution,
    ) -> AgentRunResult:
        self._raise_if_not_open("resolving a plan interaction")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        pending = self._require_pending_plan_interaction(resolution.interaction_id)
        self._raise_if_active_run()
        async with self._run_lock:
            await self._sync_mcp_servers_for_turn()
            state = self._resume_active_state(pending)
            self._prepare_state_for_plan(state)
            return await self._run_owned(
                state,
                lambda: self.wiring.agent_runtime.resume_after_plan_interaction(state, resolution),
            )

    async def resolve_mcp_elicitation(
        self,
        resolution: McpElicitationResolution,
    ) -> AgentRunResult:
        self._raise_if_not_open("resolving an MCP elicitation")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        pending = self._require_pending_mcp_elicitation(resolution.interaction_id)
        self._raise_if_active_run()
        async with self._run_lock:
            await self._sync_mcp_servers_for_turn()
            state = self._resume_active_state(pending)
            return await self._run_owned(
                state,
                lambda: self.wiring.agent_runtime.resume_after_mcp_elicitation(state, resolution),
            )

    async def resolve_mcp_input_required(
        self,
        resolution: McpInputRequiredInteractionResolution,
    ) -> AgentRunResult:
        self._raise_if_not_open("resolving MCP input-required")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        pending = self._require_pending_mcp_input_required(resolution.interaction_id)
        self._raise_if_active_run()
        async with self._run_lock:
            await self._sync_mcp_servers_for_turn()
            state = self._resume_active_state(pending)
            return await self._run_owned(
                state,
                lambda: self.wiring.agent_runtime.resume_after_mcp_input_required(state, resolution),
            )

    async def exit_plan_workflow(
        self,
        *,
        source: str,
        user_feedback: str = "",
    ) -> None:
        self._raise_if_not_open("exiting plan")
        if source not in {"user_cancel", "user_force_exit"}:
            raise ValueError("plan exit source must be user_cancel or user_force_exit")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        if not self.plan_state.active:
            raise ValueError("plan workflow is not active")
        self._raise_if_active_run()
        async with self._run_lock:
            pending = self.pending_interaction
            state = self._suspended_state
            if pending is not None:
                if not isinstance(pending, PendingPlanInteraction):
                    raise HostSessionPendingInteractionError(
                        "host session has a non-plan pending interaction; resolve or stop it before exiting plan"
                    )
                if pending.kind != "exit" and source != "user_force_exit":
                    raise HostSessionPendingInteractionError(
                        "host session has a pending plan question; answer it or use force-exit before cancelling plan"
                    )
                state = self._resume_active_state(pending)
                if pending.kind == "exit":
                    await self.wiring.runtime_wiring.runtime_session.emit(
                        PlanExitResolvedEvent(
                            run_id=state.run_id,
                            turn_id=state.turn_id,
                            reply_id=state.reply_id,
                            exit_request_id=pending.exit_request_id or "",
                            tool_call_id=pending.tool_call_id,
                            decision="cancel",
                            user_feedback=user_feedback,
                        ),
                        state=state,
                    )
            else:
                state = self.wiring.agent_runtime.new_state()
                self._prepare_state_for_plan(state)
                self.active_run_id = state.run_id
                self._active_state = state
            await self._emit_plan_mode_exited(
                state,
                source=source,
                exit_request_id=pending.exit_request_id if isinstance(pending, PendingPlanInteraction) else None,
            )
            if pending is not None:
                await self.wiring.agent_runtime.abort_run(state, reason=AbortKind.USER_STOP)
                self._suspended_state = None
                self.suspended_run_id = None
                self.pending_interaction = None
            self._finish_active_run()

    async def stream_plan_interaction_resolution(
        self,
        resolution: PlanInteractionResolution,
    ) -> AsyncIterator[AgentEvent]:
        self._raise_if_not_open("resolving a plan interaction")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        pending = self._require_pending_plan_interaction(resolution.interaction_id)
        self._raise_if_active_run()
        async with self._run_lock:
            await self._sync_mcp_servers_for_turn()
            state = self._resume_active_state(pending)
            self._prepare_state_for_plan(state)
            async for event in self._stream_owned(
                state,
                lambda: self.wiring.agent_runtime.stream_after_plan_interaction(state, resolution),
            ):
                yield event

    async def stop_current_turn(
        self,
        *,
        reason: AbortKind = AbortKind.USER_STOP,
        timeout: float = 2.0,
    ) -> AgentRunResult | None:
        self._raise_if_not_open("stopping the current turn")
        async with self._stop_lock:
            task = self._active_task
            state = self._active_state
            if self.pending_interaction is not None and (task is None or task.done()):
                if self._run_lock.locked():
                    raise HostSessionBusyError("host session already has an active run")
                async with self._run_lock:
                    pending = self.pending_interaction
                    if pending is None:
                        return None
                    state = self._require_suspended_state(pending)
                    self.active_run_id = state.run_id
                    self.stopping_run_id = state.run_id
                    self.last_active_at = time.monotonic()
                    try:
                        result = await self.wiring.agent_runtime.abort_run(state, reason=reason)
                        self._capture_pending_interaction(result.state)
                        return result
                    finally:
                        self.active_run_id = None
                        self.stopping_run_id = None
                        self.last_active_at = time.monotonic()

            if task is None or state is None:
                return None
            if task.done():
                return None
            self.stopping_run_id = state.run_id
            state.stop_request = StopRequest(reason=reason)
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except asyncio.CancelledError:
                pass
            except TimeoutError:
                return None
            except Exception:
                pass
            # The owned task (streaming drive or run coroutine) finalizes itself
            # on a stop_request; abort_run is idempotent and yields the result.
            return await self.wiring.agent_runtime.abort_run(state, reason=reason)

    def replay_events(self, *, after_sequence: int | None = None) -> list[AgentEvent]:
        return self.wiring.runtime_wiring.event_log.iter(after_sequence=after_sequence)

    def add_compaction_listener(self, listener: Callable[[AgentEvent], None]) -> None:
        """Register a best-effort observer for terminal context compaction events."""

        self._compaction_listeners.append(listener)

    async def compact_now(self) -> dict[str, object]:
        """Manually compact this idle session before the auto threshold is reached."""

        self._raise_if_not_open("compacting context")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        self._raise_if_pending_interaction("compacting context")
        self._raise_if_active_run()
        service = self.wiring.runtime_wiring.compaction_service
        if service is None:
            raise RuntimeError("context compaction is not configured for this session")
        async with self._run_lock:
            event_log = self.wiring.runtime_wiring.event_log
            before_sequence = await asyncio.to_thread(event_log.next_sequence)
            try:
                event = await service.compact(trigger="manual", reason="user_requested", force=True)
            finally:
                await self._publish_compaction_events_after(before_sequence)
            return {
                "compacted": event is not None,
                "compaction_id": event.compaction_id if event is not None else None,
                "summary_artifact_id": event.summary_artifact_id if event is not None else None,
                "window_id": event.window_id if event is not None else None,
                "through_sequence": event.through_sequence if event is not None else None,
                "keep_after_sequence": event.keep_after_sequence if event is not None else None,
            }

    # -- Close / teardown -----------------------------------------------------

    def close(self) -> None:
        """Synchronous runtime-local close. Idempotent.

        Does NOT release the shared workspace terminal lease (HostCore/supervisor
        owns that) and does NOT delete the workspace root (HostCore does that
        last, after lease release) — see contract §6.2/§7.1.
        """
        if self._lifecycle is HostSessionLifecycle.CLOSED:
            return
        self._lifecycle = HostSessionLifecycle.CLOSED
        self.pending_interaction = None
        self._suspended_state = None
        self._active_state = None
        self._active_task = None
        self.stopping_run_id = None
        self.suspended_run_id = None
        self.wiring.agent_runtime.close()

    async def aclose(
        self,
        *,
        reason: AbortKind = AbortKind.HOST_TEARDOWN,
        drain_timeout_seconds: float = 5.0,
    ) -> None:
        """Bounded, idempotent run-control close.

        Both the active run and any suspended (pending-interaction) run get a
        typed, auditable terminal RunEnd under ``reason`` (default host-teardown,
        never masqueraded as USER_STOP) instead of being silently dropped
        (contract §6.2, decision 1)."""
        if self._lifecycle is HostSessionLifecycle.CLOSED:
            return
        self.begin_close()
        await self.drain_active_run(reason=reason, timeout_seconds=drain_timeout_seconds)
        await self._finalize_suspended_run(reason)
        mcp_manager = self.wiring.runtime_wiring.mcp_manager
        if mcp_manager is not None:
            try:
                await mcp_manager.aclose(timeout_seconds=drain_timeout_seconds)
            except asyncio.CancelledError:
                # MCP SDK transports can use cancellation as an internal close
                # signal.  HostSession close owns teardown and should remain
                # idempotent; do not let a transport cancel scope poison the
                # caller task or make ``:close`` crash.
                _clear_current_task_cancellation()
        self.close()

    async def drain_active_run(
        self,
        *,
        reason: AbortKind | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        """Stop an in-flight run via the owned task handle.

        When ``reason`` is given the run is finalized with an auditable terminal
        outcome (a stop_request the owned task converts into a RunEnd); without a
        reason it is a best-effort cancel only.
        """
        task = self._active_task
        state = self._active_state
        if task is None or task.done() or task is asyncio.current_task():
            return
        if reason is not None and state is not None and state.stop_request is None:
            state.stop_request = StopRequest(reason=reason)
            self.stopping_run_id = state.run_id
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
        except (asyncio.CancelledError, TimeoutError):
            pass
        except Exception:
            pass

    async def _finalize_suspended_run(self, reason: AbortKind) -> None:
        state = self._suspended_state
        if state is None:
            return
        try:
            await self.wiring.agent_runtime.abort_run(state, reason=reason)
        except Exception:
            pass
        self._suspended_state = None
        self.pending_interaction = None
        self.suspended_run_id = None

    def summary(self) -> dict[str, object]:
        return {
            "host_session_id": self.host_session_id,
            "conversation_id": self.conversation_id,
            "runtime_session_id": self.runtime_session_id,
            "workspace_kind": self.workspace.workspace_kind,
            "workspace_root": str(self.workspace.workspace_root),
            "display_label": self.workspace.display_label,
            "created_at": self.created_at,
            "last_active_at": self.last_active_at,
            "lifecycle": self._lifecycle.value,
            "closed": self.closed,
            "active_run_id": self.active_run_id,
            "stopping_run_id": self.stopping_run_id,
            "is_stopping": self.stopping_run_id is not None,
            "suspended_run_id": self.suspended_run_id,
            "pending_approval": self.get_pending_approval().to_dict() if self.get_pending_approval() is not None else None,
            "pending_interaction": self.pending_interaction.to_dict() if self.pending_interaction is not None else None,
            "plan": self.plan_state.to_dict(),
            "has_live_processes": self.has_live_processes,
            "terminal": self.terminal_summary,
        }

    # -- Internal execution primitive -----------------------------------------

    def _begin_active_state(self) -> LoopState:
        state = self.wiring.agent_runtime.new_state()
        self._prepare_state_for_plan(state)
        self.active_run_id = state.run_id
        self._active_state = state
        self.last_active_at = time.monotonic()
        return state

    def _resume_active_state(self, pending: PendingInteraction) -> LoopState:
        state = self._require_suspended_state(pending)
        self.active_run_id = state.run_id
        self._active_state = state
        self.last_active_at = time.monotonic()
        return state

    async def _run_owned(
        self,
        state: LoopState,
        make_result: Callable[[], Awaitable[AgentRunResult]],
    ) -> AgentRunResult:
        async def _drive() -> AgentRunResult:
            try:
                try:
                    result = await make_result()
                except asyncio.CancelledError:
                    request = state.stop_request
                    if request is None:
                        raise
                    result = await self.wiring.agent_runtime.abort_run(state, reason=request.reason)
                self._capture_pending_interaction(result.state)
                self._clear_plan_entry_audit_if_emitted(result.state)
                return result
            finally:
                self._finish_active_run()

        # The owned handle covers the complete execution lifecycle, including
        # cancellation -> abort/RunEnd conversion and host bookkeeping. A closer
        # that drains this task therefore cannot release runtime/terminal resources
        # while the outer wrapper is still finalizing the run.
        task: asyncio.Task[AgentRunResult] = asyncio.create_task(_drive())
        self._active_task = task
        return await task

    async def _stream_owned(
        self,
        state: LoopState,
        make_stream: Callable[[], AsyncIterator[AgentEvent]],
    ) -> AsyncIterator[AgentEvent]:
        observer = _StreamObserver()

        async def _drive() -> None:
            try:
                async for event in make_stream():
                    await observer.emit(event)
            except asyncio.CancelledError:
                request = state.stop_request
                if request is not None:
                    async for event in self.wiring.agent_runtime.stream_abort_run(state, reason=request.reason):
                        await observer.emit(event)
                    await observer.emit(_STREAM_DONE)
                    return
                raise
            except Exception as exc:  # surface to consumer, mirror prior direct-iteration semantics
                await observer.emit(_StreamError(exc))
                await observer.emit(_STREAM_DONE)
                return
            else:
                await observer.emit(_STREAM_DONE)
            finally:
                self._capture_pending_interaction(state)
                self._clear_plan_entry_audit_if_emitted(state)
                self._finish_active_run()

        task = asyncio.create_task(_drive())
        self._active_task = task
        stream_error: BaseException | None = None
        try:
            while True:
                item = await observer.queue.get()
                if item is _STREAM_DONE:
                    await asyncio.shield(task)
                    if stream_error is not None:
                        raise stream_error
                    break
                if isinstance(item, _StreamError):
                    stream_error = item.exc
                    continue
                yield item
        finally:
            # Closing or abandoning the transport-facing generator only detaches
            # this observer. HostSession remains the execution owner; explicit
            # stop/session close/HostCore shutdown are the cancellation paths.
            observer.detach()

    def _finish_active_run(self) -> None:
        self._notify_governance()
        self._active_task = None
        self._active_state = None
        self.active_run_id = None
        self.stopping_run_id = None
        self.last_active_at = time.monotonic()

    async def _prepare_prior_messages_for_turn(self, user_input: str):
        prior_messages = self._prior_messages()
        service = self.wiring.runtime_wiring.compaction_service
        if service is not None:
            compacted = await self._compact_if_needed_and_notify(
                service,
                current_user_input=user_input,
                model_visible_messages=prior_messages,
                reason="preflight_context_threshold",
            )
            if compacted:
                prior_messages = self._prior_messages()
        return prior_messages

    def _prior_messages(self):
        runtime_wiring = self.wiring.runtime_wiring
        return rebuild_prior_messages(
            runtime_wiring.event_log,
            archive=runtime_wiring.archive,
            session_id=runtime_wiring.runtime_session.runtime_session_id,
        )

    async def _compact_if_needed_and_notify(self, service, **kwargs) -> bool:
        event_log = self.wiring.runtime_wiring.event_log
        before_sequence = await asyncio.to_thread(event_log.next_sequence)
        compacted = await service.compact_if_needed(**kwargs)
        compaction_events = await self._publish_compaction_events_after(before_sequence)
        terminal_event = self._latest_terminal_compaction_event(compaction_events)
        if terminal_event is not None:
            self._notify_compaction_listeners(terminal_event)
        return compacted

    async def _publish_compaction_events_after(self, before_sequence: int) -> list[AgentEvent]:
        compaction_events = await asyncio.to_thread(self._compaction_events_after, before_sequence - 1)
        self.wiring.runtime_wiring.runtime_session.publish_stored_events(compaction_events)
        return compaction_events

    def _compaction_events_after(self, after_sequence: int) -> list[AgentEvent]:
        return [
            event
            for event in self.wiring.runtime_wiring.event_log.iter(after_sequence=after_sequence)
            if isinstance(
                event,
                (
                    ContextCompactionStartedEvent,
                    ContextCompactionCompletedEvent,
                    ContextCompactionFailedEvent,
                ),
            )
        ]

    def _latest_terminal_compaction_event(self, events: list[AgentEvent]) -> AgentEvent | None:
        terminal_events = [
            event
            for event in events
            if isinstance(event, (ContextCompactionCompletedEvent, ContextCompactionFailedEvent))
        ]
        return terminal_events[-1] if terminal_events else None

    def _notify_compaction_listeners(self, event: AgentEvent) -> None:
        for listener in list(self._compaction_listeners):
            try:
                listener(event)
            except Exception:
                continue

    def _capture_pending_interaction(self, state: LoopState) -> None:
        if state.status is LoopStatus.WAITING_USER:
            if state.pending_interaction_kind == "plan":
                self.pending_interaction = pending_plan_interaction_from_state(state, self.host_session_id)
            elif state.pending_interaction_kind == "mcp_elicitation":
                self.pending_interaction = pending_mcp_elicitation_from_state(state, self.host_session_id)
            elif state.pending_interaction_kind == "mcp_input_required":
                self.pending_interaction = pending_mcp_input_required_from_state(state, self.host_session_id)
            else:
                self.pending_interaction = pending_approval_from_state(state, self.host_session_id)
            self._suspended_state = state
            self.suspended_run_id = state.run_id
            return
        self.pending_interaction = None
        self._suspended_state = None
        self.suspended_run_id = None

    def _notify_governance(self) -> None:
        coordinator = self.wiring.runtime_wiring.governance_coordinator
        engine = self.wiring.runtime_wiring.memory_governance_engine
        if coordinator is not None and engine is not None:
            coordinator.notify(engine)

    def _require_pending_approval(self, approval_id: str) -> PendingApproval:
        pending = self.get_pending_approval()
        if pending is None:
            raise ValueError("host session has no pending approval")
        if pending.approval_id != approval_id:
            raise ValueError("approval id does not match the pending approval")
        return pending

    def _require_pending_plan_interaction(self, interaction_id: str) -> PendingPlanInteraction:
        pending = self.pending_interaction
        if not isinstance(pending, PendingPlanInteraction):
            raise ValueError("host session has no pending plan interaction")
        if pending.interaction_id != interaction_id:
            raise ValueError("plan interaction id does not match the pending interaction")
        return pending

    def _require_pending_mcp_elicitation(self, interaction_id: str) -> PendingMcpElicitation:
        pending = self.pending_interaction
        if not isinstance(pending, PendingMcpElicitation):
            raise ValueError("host session has no pending MCP elicitation")
        if pending.interaction_id != interaction_id:
            raise ValueError("MCP elicitation id does not match the pending interaction")
        return pending

    def _require_pending_mcp_input_required(self, interaction_id: str) -> PendingMcpInputRequired:
        pending = self.pending_interaction
        if not isinstance(pending, PendingMcpInputRequired):
            raise ValueError("host session has no pending MCP input-required interaction")
        if pending.interaction_id != interaction_id:
            raise ValueError("MCP input-required id does not match the pending interaction")
        return pending

    def _require_suspended_state(self, pending: PendingInteraction) -> LoopState:
        if self._suspended_state is None:
            raise ValueError("host session has no suspended state")
        if self._suspended_state.run_id != pending.run_id:
            raise ValueError("suspended state does not match pending interaction")
        return self._suspended_state

    def enter_plan(self, *, reason: str = "") -> EffectivePermissionPolicy:
        """Host/user entry point for Plan mode.

        This is the :plan / Plan button path: no control run is created, but
        permission is synchronously narrowed before the next model turn.
        """
        self._raise_if_not_open("entering plan")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        self._raise_if_pending_interaction("entering plan")
        self._raise_if_active_run()
        if not self.plan_state.active:
            self.plan_state.begin(
                source="user",
                previous_mode=self.current_permission_mode,
                previous_policy=self.current_permission_policy(),
                reason=reason,
                pending_entry_audit=False,
            )
            self._emit_user_plan_mode_entered(reason=reason)
        policy = preset_to_policy(PermissionMode.READ_ONLY)
        self.wiring.agent_runtime.set_permission_policy(policy, mode=PermissionMode.READ_ONLY)
        self.last_active_at = time.monotonic()
        return policy

    def _emit_user_plan_mode_entered(self, *, reason: str = "") -> AgentEvent:
        suffix = uuid4().hex
        return self.wiring.runtime_wiring.runtime_session.emit_from_thread(
            PlanModeEnteredEvent(
                run_id=f"run:host-plan-entry:{suffix}",
                turn_id=f"turn:host-plan-entry:{suffix}",
                reply_id=f"reply:host-plan-entry:{suffix}",
                source="user",
                previous_permission_mode=self.plan_state.pre_plan_permission_mode,
                previous_permission_policy=self.plan_state.pre_plan_permission_policy or {},
                reason=reason,
            )
        )

    def _plan_runtime_messages(self):
        if not self.plan_state.active:
            return []
        if self.plan_state.pending_entry_audit:
            return [
                SystemMsg(
                    PLAN_ENTRY_INSTRUCTION_NAME,
                    PLAN_ENTRY_INSTRUCTION,
                    metadata={"runtime_instruction": "plan_entry"},
                )
            ]
        return [
            SystemMsg(
                PLAN_ACTIVE_INSTRUCTION_NAME,
                PLAN_ACTIVE_INSTRUCTION,
                metadata={"runtime_instruction": "plan_active"},
            )
        ]

    def _prepare_state_for_plan(self, state: LoopState) -> None:
        state.scratchpad["host_session_id"] = self.host_session_id
        state.scratchpad["plan_state"] = self.plan_state
        if self.plan_state.active:
            state.scratchpad["plan_active"] = True
        if self.plan_state.pending_entry_audit:
            state.scratchpad["plan_entry_audit"] = {
                "source": "user",
                "previous_permission_mode": self.plan_state.pre_plan_permission_mode,
                "previous_permission_policy": self.plan_state.pre_plan_permission_policy or {},
                "reason": self.plan_state.entry_reason,
            }

    def _clear_plan_entry_audit_if_emitted(self, state: LoopState) -> None:
        if state.scratchpad.get("plan_entry_audit_emitted"):
            self.plan_state.pending_entry_audit = False

    def _pre_plan_policy(self) -> EffectivePermissionPolicy:
        payload = self.plan_state.pre_plan_permission_policy or {}
        if not payload:
            return default_permission_policy()
        return EffectivePermissionPolicy(
            profile=PermissionProfile(str(payload["profile"])),
            approval=ApprovalPolicy(str(payload["approval_policy"])),
            terminal=TerminalAccess(str(payload["terminal_access"])),
            execution_boundary="host",
            network_isolated=bool(payload.get("network_isolated", False)),
        )

    async def _emit_plan_mode_exited(
        self,
        state: LoopState,
        *,
        source: str,
        exit_request_id: str | None = None,
    ) -> None:
        restored_mode = self.plan_state.pre_plan_permission_mode
        restored_policy = self._pre_plan_policy()
        self.wiring.agent_runtime.set_permission_policy(
            restored_policy,
            mode=parse_permission_mode(restored_mode) if restored_mode is not None else None,
        )
        await self.wiring.runtime_wiring.runtime_session.emit(
            PlanModeExitedEvent(
                run_id=state.run_id,
                turn_id=state.turn_id,
                reply_id=state.reply_id,
                source=source,  # type: ignore[arg-type]
                exit_request_id=exit_request_id,
                restored_permission_mode=restored_mode,
                restored_permission_policy=restored_policy.to_dict(),
            ),
            state=state,
        )
        self.plan_state.finish()

    def _raise_if_not_open(self, action: str) -> None:
        if self._lifecycle is not HostSessionLifecycle.OPEN:
            raise RuntimeError(f"host session is closed; cannot {action}")

    def _raise_if_pending_interaction(self, action: str) -> None:
        pending = self.pending_interaction
        if pending is None:
            return
        if isinstance(pending, PendingApproval):
            raise HostSessionPendingApprovalError(
                f"host session has a pending approval; resolve or deny it before {action}"
            )
        raise HostSessionPendingInteractionError(
            f"host session has a pending user interaction; resolve or stop it before {action}"
        )

    def _raise_if_active_run(self) -> None:
        task = self._active_task
        if self._run_lock.locked() or (task is not None and not task.done()):
            raise HostSessionBusyError("host session already has an active run")


def _clear_current_task_cancellation() -> None:
    task = asyncio.current_task()
    if task is None or not hasattr(task, "uncancel"):
        return
    while task.cancelling():
        task.uncancel()
