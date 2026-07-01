"""Reusable host facade for web, desktop, and thin CLI drivers."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pulsara_agent.host.identity import HostWorkspaceInput, ResolvedWorkspace, resolve_workspace
from pulsara_agent.host.registry import HostSessionRegistry, HostSessionSummary
from pulsara_agent.host.session import HostSession
from pulsara_agent.host.supervisor import (
    WorkspaceClosingError,
    WorkspaceTerminalSnapshot,
    WorkspaceTerminalSupervisor,
)
from pulsara_agent.llm import ModelRole
from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.runtime.approval import ApprovalResolution, PendingApproval
from pulsara_agent.runtime.agent import AgentRunResult
from pulsara_agent.runtime.permission import EffectivePermissionPolicy
from pulsara_agent.runtime.plan import PendingInteraction, PlanInteractionResolution
from pulsara_agent.runtime.recovery import AbortKind
from pulsara_agent.runtime.terminal import (
    BorrowedWorkspaceTerminalRuntime,
    TerminalOwnerContext,
    WorkspaceTerminalLease,
)
from pulsara_agent.runtime.wiring import build_agent_runtime_wiring
from pulsara_agent.retrieval.runtime import RetrievalRuntimeResources, build_retrieval_runtime_resources
from pulsara_agent.memory.canonical.vector_index_sync import MemoryVectorIndexSync
from pulsara_agent.memory.canonical.vector_worker import MemoryVectorIndexWorker
from pulsara_agent.memory.governance.coordinator import MemoryGovernanceCoordinator
from pulsara_agent.settings import PulsaraSettings


class HostCoreLifecycle(StrEnum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass(slots=True)
class HostCore:
    settings: PulsaraSettings
    durable: bool = True
    scratch_root: Path | None = None
    registry: HostSessionRegistry = field(default_factory=HostSessionRegistry)
    _supervisors: dict[str, WorkspaceTerminalSupervisor] = field(default_factory=dict, init=False, repr=False)
    _session_leases: dict[str, WorkspaceTerminalLease] = field(default_factory=dict, init=False, repr=False)
    _supervisor_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _retrieval_resources: RetrievalRuntimeResources | None = field(default=None, init=False, repr=False)
    _retrieval_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _governance_coordinator: MemoryGovernanceCoordinator | None = field(default=None, init=False, repr=False)
    _lifecycle: HostCoreLifecycle = field(default=HostCoreLifecycle.OPEN, init=False, repr=False)
    _lifecycle_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _session_close_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _session_close_events: dict[str, asyncio.Event] = field(default_factory=dict, init=False, repr=False)
    _shutdown_complete: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    # -- Lifecycle gate -------------------------------------------------------

    @property
    def lifecycle(self) -> HostCoreLifecycle:
        return self._lifecycle

    def _raise_if_not_accepting(self, action: str) -> None:
        if self._lifecycle is not HostCoreLifecycle.OPEN:
            raise RuntimeError(f"HostCore is {self._lifecycle.value}; cannot {action}")

    # -- Open (a single, rollback-safe transaction) ---------------------------

    async def open_session(
        self,
        workspace_input: HostWorkspaceInput,
        *,
        conversation_id: str | None = None,
        host_session_id: str | None = None,
        model_role: ModelRole = ModelRole.PRO,
        options: LLMOptions | None = None,
        system_prompt: str | None = None,
        memory_reflection: bool = True,
        permission_policy: EffectivePermissionPolicy | None = None,
    ) -> HostSession:
        self._raise_if_not_accepting("open a session")
        workspace = resolve_workspace(workspace_input, scratch_root=self.scratch_root)
        host_session_id = host_session_id or f"host:{uuid4().hex}"
        conversation_id = conversation_id or f"conversation:{uuid4().hex}"
        # Reserve identity first: fail-closed on duplicate id before any resource
        # is built, so two borrowers can never share a terminal owner principal.
        reservation = await self.registry.reserve(host_session_id, conversation_id)
        lease: WorkspaceTerminalLease | None = None
        wiring = None
        try:
            lease = await self._attach_supervisor(workspace, host_session_id, conversation_id)
            wiring = build_agent_runtime_wiring(
                self.settings,
                workspace.workspace_root,
                durable=self.durable,
                model_role=model_role,
                options=options,
                system_prompt=system_prompt,
                memory_domain=workspace.memory_domain,
                memory_reflection=memory_reflection,
                terminal_binding=BorrowedWorkspaceTerminalRuntime(
                    owner=TerminalOwnerContext(
                        host_session_id=host_session_id,
                        conversation_id=conversation_id,
                    ),
                    manager=lease.manager,
                ),
                permission_policy=permission_policy,
                retrieval_resources=await self._get_retrieval_resources() if self.durable else None,
                governance_coordinator=self._governance_coordinator if self.durable else None,
            )
            session = HostSession(
                host_session_id=host_session_id,
                conversation_id=conversation_id,
                workspace=workspace,
                wiring=wiring,
                terminal_lease=lease,
            )
            # Publish under the lifecycle lock and re-check state: this is the
            # linearization point against shutdown/close. If shutdown won the
            # race, we roll back instead of leaking a session into a closed
            # HostCore (contract §0, audit P0-2).
            async with self._lifecycle_lock:
                if self._lifecycle is not HostCoreLifecycle.OPEN:
                    raise RuntimeError("HostCore is closing; open aborted")
                async with self._supervisor_lock:
                    supervisor = self._supervisors.get(lease.workspace_key)
                    if supervisor is None or not supervisor.is_current_lease(lease):
                        raise WorkspaceClosingError(
                            f"workspace {lease.workspace_key} closed while session was opening"
                        )
                    await self.registry.publish(reservation, session)
                    self._session_leases[host_session_id] = lease
            return session
        except BaseException:
            # Rollback exactly once: release the reservation, release the lease
            # (kills/prunes any owner state), and close partially-built runtime
            # resources so a failed open leaks nothing (contract §0, audit P0-4).
            await self.registry.release_reservation(reservation)
            if lease is not None:
                await self._release_supervisor_lease(lease)
            if wiring is not None:
                wiring.agent_runtime.close()
            raise

    # -- Read facades (allowed during CLOSING for diagnostics) ----------------

    async def get_session(self, host_session_id: str) -> HostSession:
        return await self.registry.get(host_session_id)

    async def find_by_conversation(self, conversation_id: str) -> HostSession | None:
        return await self.registry.find_by_conversation(conversation_id)

    async def replay_events(self, host_session_id: str, *, after_sequence: int | None = None):
        session = await self.get_session(host_session_id)
        return session.replay_events(after_sequence=after_sequence)

    async def get_pending_approval(self, host_session_id: str) -> PendingApproval | None:
        session = await self.get_session(host_session_id)
        return session.get_pending_approval()

    async def get_pending_interaction(self, host_session_id: str) -> PendingInteraction | None:
        session = await self.get_session(host_session_id)
        return session.get_pending_interaction()

    async def list_sessions(self) -> list[HostSessionSummary]:
        return await self.registry.list_sessions()

    async def list_workspace_terminal_snapshots(self) -> list[WorkspaceTerminalSnapshot]:
        async with self._supervisor_lock:
            supervisors = list(self._supervisors.values())
        return [supervisor.snapshot() for supervisor in supervisors]

    async def list_workspace_supervisors(self) -> list[dict[str, object]]:
        return [snapshot.to_payload() for snapshot in await self.list_workspace_terminal_snapshots()]

    # -- Execution facades (gated: never start/continue a run while closing) --

    async def resolve_approval(
        self,
        host_session_id: str,
        resolution: ApprovalResolution,
    ) -> AgentRunResult:
        self._raise_if_not_accepting("resolve an approval")
        session = await self.get_session(host_session_id)
        return await session.resolve_approval(resolution)

    async def resolve_plan_interaction(
        self,
        host_session_id: str,
        resolution: PlanInteractionResolution,
    ) -> AgentRunResult:
        self._raise_if_not_accepting("resolve a plan interaction")
        session = await self.get_session(host_session_id)
        return await session.resolve_plan_interaction(resolution)

    async def stop_current_turn(
        self,
        host_session_id: str,
        *,
        reason: AbortKind = AbortKind.USER_STOP,
    ) -> AgentRunResult | None:
        self._raise_if_not_accepting("stop the current turn")
        session = await self.get_session(host_session_id)
        return await session.stop_current_turn(reason=reason)

    async def set_permission_mode(
        self,
        host_session_id: str,
        mode: str,
    ):
        self._raise_if_not_accepting("switch permission mode")
        session = await self.get_session(host_session_id)
        return session.set_permission_mode(mode)

    async def enter_plan(self, host_session_id: str, *, reason: str = ""):
        self._raise_if_not_accepting("enter plan")
        session = await self.get_session(host_session_id)
        return session.enter_plan(reason=reason)

    async def stream_approval_resolution(self, host_session_id: str, resolution: ApprovalResolution):
        self._raise_if_not_accepting("resolve an approval")
        session = await self.get_session(host_session_id)
        async for event in session.stream_approval_resolution(resolution):
            yield event

    async def stream_plan_interaction_resolution(
        self,
        host_session_id: str,
        resolution: PlanInteractionResolution,
    ):
        self._raise_if_not_accepting("resolve a plan interaction")
        session = await self.get_session(host_session_id)
        async for event in session.stream_plan_interaction_resolution(resolution):
            yield event

    # -- Idle sweep (HostCore is the sole closer; registry only discovers) ----

    async def sweep_idle(self, *, now: float | None = None) -> list[str]:
        candidates = await self.registry.list_idle_candidates(now=now)
        for host_session_id in candidates:
            await self.close_session(host_session_id)
        return candidates

    # -- Close paths (the three flows share one close primitive) --------------

    async def close_session(self, host_session_id: str) -> None:
        """Sole session-close coordinator. Concurrent callers await one close."""
        async with self._session_close_lock:
            session = await self.registry.begin_close(host_session_id)
            if session is None:
                close_event = self._session_close_events.get(host_session_id)
                is_closer = False
            else:
                close_event = asyncio.Event()
                self._session_close_events[host_session_id] = close_event
                is_closer = True
        if not is_closer:
            if close_event is not None:
                await close_event.wait()
            return
        lease = self._session_leases.pop(host_session_id, None)
        errors: list[BaseException] = []
        try:
            try:
                await session.aclose(reason=AbortKind.HOST_TEARDOWN)
            except BaseException as exc:
                errors.append(exc)
            if lease is not None:
                try:
                    await self._release_supervisor_lease(lease)
                except BaseException as exc:
                    errors.append(exc)
            try:
                await self.registry.finish_close(host_session_id)
            except BaseException as exc:
                errors.append(exc)
            try:
                self._cleanup_workspace_root(session.workspace)
            except BaseException as exc:
                errors.append(exc)
        finally:
            assert close_event is not None
            close_event.set()
            async with self._session_close_lock:
                self._session_close_events.pop(host_session_id, None)
        if errors:
            raise errors[0]

    async def close_workspace(self, workspace_key: str) -> None:
        async with self._supervisor_lock:
            supervisor = self._supervisors.get(workspace_key)
            if supervisor is not None:
                supervisor.mark_closing()
        for summary in await self.registry.list_sessions():
            try:
                session = await self.registry.get(summary.host_session_id)
            except KeyError:
                continue
            if session.workspace.workspace_key == workspace_key:
                await self.close_session(summary.host_session_id)
        await self._shutdown_supervisor(workspace_key)

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            if self._lifecycle is HostCoreLifecycle.CLOSED:
                return
            if self._lifecycle is HostCoreLifecycle.CLOSING:
                is_shutdown_owner = False
            else:
                self._lifecycle = HostCoreLifecycle.CLOSING
                is_shutdown_owner = True
        if not is_shutdown_owner:
            await self._shutdown_complete.wait()
            return
        errors: list[BaseException] = []
        try:
            summaries = await self.registry.list_sessions()
            # Close every session first so each run's finally completes
            # (governance notify) and each terminal lease is released, before
            # retrieval providers close — no recoverable/startable borrower may
            # outlive the provider, and no terminal tool may outlive the all-kill
            # (contract §7.3).
            for summary in summaries:
                try:
                    await self.close_session(summary.host_session_id)
                except BaseException as exc:
                    errors.append(exc)
            async with self._supervisor_lock:
                supervisors = list(self._supervisors.values())
                self._supervisors.clear()
            for supervisor in supervisors:
                supervisor.mark_closed()
                try:
                    await asyncio.to_thread(supervisor.terminal_sessions.shutdown)
                except BaseException as exc:
                    errors.append(exc)
            async with self._retrieval_lock:
                resources = self._retrieval_resources
                self._retrieval_resources = None
                self._governance_coordinator = None
            if resources is not None:
                try:
                    await resources.aclose()
                except BaseException as exc:
                    errors.append(exc)
        except BaseException as exc:
            # Even an unexpected coordinator failure must not strand every
            # concurrent waiter behind a permanently-CLOSING HostCore.
            errors.append(exc)
        finally:
            async with self._lifecycle_lock:
                self._lifecycle = HostCoreLifecycle.CLOSED
                self._shutdown_complete.set()
        if errors:
            raise errors[0]

    # -- Internal helpers -----------------------------------------------------

    async def _get_retrieval_resources(self) -> RetrievalRuntimeResources:
        async with self._retrieval_lock:
            if self._lifecycle is not HostCoreLifecycle.OPEN:
                raise RuntimeError("HostCore is closing; cannot start retrieval resources")
            if self._retrieval_resources is None:
                self._retrieval_resources = build_retrieval_runtime_resources(self.settings.retrieval)
                self._governance_coordinator = MemoryGovernanceCoordinator()
                self._retrieval_resources.attach_worker(self._governance_coordinator)
                if self._retrieval_resources.embedding is not None:
                    vector_worker = MemoryVectorIndexWorker(
                        MemoryVectorIndexSync(
                            dsn=self.settings.storage.postgres_dsn,
                            provider=self._retrieval_resources.embedding,
                            provider_name=self.settings.retrieval.embedding.provider,
                        )
                    )
                    self._governance_coordinator.on_commit = vector_worker.wake
                    self._retrieval_resources.attach_worker(vector_worker)
                self._retrieval_resources.start()
            return self._retrieval_resources

    async def _attach_supervisor(
        self,
        workspace: ResolvedWorkspace,
        host_session_id: str,
        conversation_id: str,
    ) -> WorkspaceTerminalLease:
        async with self._supervisor_lock:
            supervisor = self._supervisors.get(workspace.workspace_key)
            if supervisor is None:
                supervisor = WorkspaceTerminalSupervisor(
                    workspace_key=workspace.workspace_key,
                    workspace_root=workspace.workspace_root,
                )
                self._supervisors[workspace.workspace_key] = supervisor
            return supervisor.attach(
                TerminalOwnerContext(
                    host_session_id=host_session_id,
                    conversation_id=conversation_id,
                )
            )

    async def _release_supervisor_lease(self, lease: WorkspaceTerminalLease) -> None:
        """Release one lease: state transition under the lock, blocking kill in a
        thread off the lock, then prune the supervisor if it is empty and open."""
        async with self._supervisor_lock:
            supervisor = self._supervisors.get(lease.workspace_key)
            owner_id = supervisor.release_lease(lease) if supervisor is not None else None
        if owner_id is not None:
            await asyncio.to_thread(lease.manager.release_owner, owner_id)
        async with self._supervisor_lock:
            supervisor = self._supervisors.get(lease.workspace_key)
            if (
                supervisor is not None
                and supervisor.is_accepting
                and supervisor.owner_count == 0
                and supervisor.terminal_sessions.live_process_count() == 0
            ):
                self._supervisors.pop(lease.workspace_key, None)

    async def _shutdown_supervisor(self, workspace_key: str) -> None:
        async with self._supervisor_lock:
            supervisor = self._supervisors.pop(workspace_key, None)
            if supervisor is None:
                return
            supervisor.mark_closed()
            manager = supervisor.terminal_sessions
        await asyncio.to_thread(manager.shutdown)

    def _cleanup_workspace_root(self, workspace: ResolvedWorkspace) -> None:
        # Transient root deletion is last, after every borrower/resource release
        # (contract §7.1): kill happened during lease release, so the directory is
        # safe to remove now.
        if workspace.cleanup_workspace_root_on_close:
            shutil.rmtree(workspace.workspace_root, ignore_errors=True)
